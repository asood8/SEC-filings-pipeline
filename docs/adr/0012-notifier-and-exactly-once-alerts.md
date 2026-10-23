# ADR-0012: Notifier and exactly-once alerts

- **Status:** Accepted
- **Date:** 2026-10-06

## Context

The notifier consumes `filings.ready` and alerts subscribers through Discord, Slack, or email. The
brief requires retries, a dead-letter queue, and "one alert per subscription, even after a replay".

Three facts shape the design:

- **Deliveries can repeat.** Kafka delivers at least once, and replays re-read history on purpose.
- **Webhooks have no idempotency keys.** A webhook call can't be safely repeated.
- **Targets come from users.** A subscription's target is a URL or address the user typed, so the
  notifier must never be steerable to arbitrary hosts.

Sign-in and subscription management (the API side of Phase 4) are decided separately.

## Decision

- **A `notifications` row per (subscription, filing)**, with a unique constraint. It is the record
  of whether that alert was delivered: `pending`, `sent`, or `failed`, plus the attempt count and
  the last error.
- **Deliver under a row lock, only if pending.** Each alert is inserted (ignoring conflicts), then
  locked with `SELECT ... FOR UPDATE`. It is sent only if still pending, and its status is
  committed in the same transaction. Redeliveries, replays, and concurrent notifier replicas
  therefore never alert a subscription twice about one filing.
- **Classify failures:**
  - **Permanent:** a 4xx from a webhook (deleted or invalid), a refused email recipient, an
    unexpected redirect, or an invalid target. The alert is marked `failed` and never retried.
  - **Transient:** a 429, a 5xx, network errors, or temporary SMTP errors. The alert stays
    `pending`. After the event's other subscriptions are handled, the handler raises so the
    consume loop retries, and only what's still pending gets sent. If retries run out, the event
    goes to the DLQ, and a replay delivers the rest later.
- **Status:** a filing moves `ready → notified` once every alert is settled.
- **Matching:** a subscription matches the filing's company or, for a Form 4, the issuer whose
  stock was traded (following Lennar catches Berkshire's Form 4 about buying Lennar). Its
  `form_types` must include the filing's form; empty means all.
- **Only new filings** (added 2026-10-06): a subscription matches only filings discovered after it
  was created. Without this, replaying `filings.ready` would send a newer subscription an alert
  for every past filing, since it has no notification rows for them yet.
- **Target safety:**
  - Webhook URLs must match official Discord or Slack webhook patterns over https, and redirects
    are never followed. User input can't make the notifier request internal addresses, such as the
    cloud metadata endpoint (SSRF).
  - Discord's `allowed_mentions` is empty, so summary text can't ping `@everyone`.
  - Slack text is escaped.
  - Email addresses that could inject headers are rejected.
- **Email backends:** SMTP with STARTTLS, which Oracle Cloud Email Delivery offers on the free tier
  (`EMAIL_BACKEND=smtp`), or `log` for development. STARTTLS checks the server's certificate and
  name (fixed 2026-10-22: Python's `smtplib` checks neither unless given a context, so anyone on
  the network path could have read the SMTP password). The log backend writes the email to the log
  instead of sending it, so no mail-catcher service is needed. It masks the recipient
  (`a***@example.com`; added 2026-10-07) and names the filing's accession number: logs are kept
  and may be shipped elsewhere, where users' addresses don't belong, and the first letter, the
  domain, and the filing are enough to follow an alert.

### Form 4 filters (added 2026-10-22)

Most Form 4s report routine events (shares withheld for taxes, stock awards, option exercises,
gifts), so an alert on every one is mostly noise. A subscription can narrow its Form 4 alerts:

- `form4_trades`: `all` (the default, and every existing subscription), `open_market` (the
  filing reports a stock purchase or sale, SEC codes P and S, the same lines the insider pages
  total), or `purchases` (code P).
- `form4_min_value`: only when those trades total at least this many US dollars (shares x
  price). With `all`, a minimum still needs open-market trades, the only ones with a market
  value.

The rule lives in `filings_common.alert_filters` and runs in the notifier after the usual
company and form-type match; other forms ignore both settings. A price given only in a footnote
is unknown, and such a trade counts as meeting the minimum: missing a large trade is worse than
one extra alert. The filters change who is alerted about a filing, not how often, so the
exactly-once rule above still holds (migration 478b6b568519 adds the columns with check
constraints).

### 8-K item filters (added 2026-10-22)

An 8-K can report anything from earnings (2.02) to a bylaw change (5.03). A subscription can
list the items it wants (`form8k_items`); an 8-K is alerted when it reports any of them, and the
empty list (the default, and every existing subscription) means every 8-K. The API accepts any
item in SEC's list; the website offers the 15 that say what happened, leaving out 9.01
(financial statements and exhibits), which comes with most 8-Ks. Migration be5a237b919d.

### Keyword alerts (added 2026-10-22)

A subscription can carry a `query`: it is alerted only about filings whose search document (the
summary, as `filing_search` indexes it) matches, with the search's syntax (`websearch_to_tsquery`:
quotes, OR, -word; "buybacks" matches "buyback"). Its company becomes optional: without one, any
watchlist company's matching filings. The notifier adds one condition to its subscription query,
an `EXISTS` on the filing's search document, which the summarizer writes before it announces the
filing. Every subscription still needs a subject (a company, a query, or both: check
`ck_subscriptions_subject`), and the unique constraint now includes the query with NULLs equal
(`NULLS NOT DISTINCT`), so the same keyword alert can't be added twice. A query of only stop
words ("the") is refused, since it would never match. Exactly-once delivery is unchanged: it is
per subscription and filing, whatever selected them. Migration a77c95f05ece.

### Previewing an alert (added 2026-10-22)

With filters and keywords, it is hard to guess what an alert will send. `GET
/subscriptions/preview` takes an alert's settings (those of `POST /subscriptions/batch`, without
destinations) and answers with the summarized filings of the last `days` (default 90) it would
have alerted about: how many, and the newest ten. The website's alert form has a "Preview the
last 90 days" button; any change to the form clears the preview, so it never describes other
settings. It matches as the notifier does: a company's alerts include Form 4s others file with it
as the issuer, keywords go through the same `websearch_to_tsquery` on the search document, and
the Form 4 and 8-K filters are the same `filings_common.alert_filters` functions, so a preview
can't drift from what is sent. It reads at most the newest 1,000 candidates (`capped` says when
there were more, making the count a lower bound), needs sign-in like every alert route, and saves
nothing. On the dev data: Apple alerts would have sent 15 filings in 90 days, open-market trades
only 8, and open-market trades of $10 million or more (Apple and NVIDIA) 4.

## Alternatives considered

- **Dedupe in Redis.** It's lost on eviction or restart, and can't show what was delivered. The
  database row is durable and auditable.
- **Claim with a `sending` status instead of a row lock.** That needs a reaper for alerts stuck in
  `sending` after a crash. The lock is released automatically if the process dies.
- **A mail-catcher container (Mailpit) for development.** One more service for little gain; the
  log backend shows the same thing.

## Consequences

- **Live check on real data.** With a test subscription to Apple, the notifier processed all 275
  `filings.ready` events in 4 seconds and logged exactly 14 alerts, one per Apple filing. After
  rewinding its consumer group to the start (a full replay), it sent zero new alerts.
- **One remaining duplicate window.** A crash after a successful send but before the commit can
  resend that one alert on redelivery. Webhook APIs offer no idempotency keys, so that window can't
  be closed; it is as small as a single HTTP call.
- **Lock duration.** A row lock is held during one HTTP call (10 s timeout) or SMTP send (20 s).
  That's fine at this volume.
- **Still to come in Phase 4:** sign-in (Google OIDC) and the subscription API, which must apply the
  same target validation when subscriptions are created. (Both done on 2026-10-06: sign-in is
  ADR-0013, and the subscriptions API validates targets with the notifier's own checks,
  `filings_common.targets`.)
