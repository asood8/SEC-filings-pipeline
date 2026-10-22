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
  (`EMAIL_BACKEND=smtp`), or `log` for development. The log backend writes the email to the log
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
