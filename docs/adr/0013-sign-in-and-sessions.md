# ADR-0013: Sign-in with Google and sessions

- **Status:** Accepted
- **Date:** 2026-10-06

## Context

Subscriptions belong to users, so Phase 4 needs sign-in. The brief chose Google OIDC and the
`users(id, email, google_sub, created_at)` table (ADR-0012 left sign-in to this decision).

The constraints:

- **The website is a static export** on Cloudflare Pages. It has no server of its own to run an
  OAuth redirect flow or to hold a client secret.
- **The API serves public data from a shared cache** keyed by URL (ADR-0011). Once some
  responses depend on who is asking, the cache must never hand one user's response to another.
- **Cookies bring CSRF.** If the browser sends credentials automatically, other sites can try to
  make requests that carry them.
- **$0 and no new infrastructure.** Sessions must live in services the project already runs.

## Decision

### Sign-in

- **Google Identity Services on the website.** Its "Sign in with Google" button returns an **ID
  token** to page script, which POSTs it as `{"credential": "..."}` to `POST /auth/google`. There
  is no redirect flow and no client secret; the API only needs the public client ID
  (`GOOGLE_CLIENT_ID`). Without it, `/auth/google` returns 503 and the rest of the API works.
- **The API verifies the token itself with PyJWT**:
  - The signature must be RS256 (never `none` or HS256), from one of the keys in Google's JWKS.
  - `aud` must be exactly our client ID; `iss` must be Google; `exp` and `iat` are checked with
    30 s of leeway; `email_verified` must be true; `sub` and `email` must be present.
  - The `alg` header is checked before any key lookup, so junk tokens never cause a fetch.
- **Google's keys are fetched with httpx2** (ADR-0005) and cached in memory for Google's
  Cache-Control `max-age`, clamped to between 5 minutes and a day. A token naming an unknown key
  (Google rotates them) triggers a refetch, but at most once a minute, so a flood of junk tokens
  can't make the API hammer Google. If a refetch fails, the last good keys stay in use.
- **Users are matched by Google's `sub`, never by email.** Addresses can change, and a freed
  address can be reassigned to someone else. When a user's address changes, the next sign-in
  updates `users.email` and moves their email subscriptions to it: email alerts only ever go to
  the account's own address (the subscriptions API enforces that).
- **Invalid tokens get a generic 401.** The reason (expired, wrong audience) is logged; the token
  never is.

### Sessions

- **Database sessions with an opaque cookie.** A sign-in creates a `sessions` row and sets a
  cookie holding a random 256-bit token (`secrets.token_urlsafe(32)`). The table stores only the
  token's SHA-256 hash, so a leaked table or backup can't be replayed as sessions.
- **The cookie is `HttpOnly`** (page scripts, including injected ones, can't read it),
  **`SameSite=Lax`** (browsers don't send it on cross-site POSTs), **`Secure`** (configurable for
  plain-http development), `Path=/`, and lasts 7 days (`API_SESSION_LIFETIME_DAYS`).
- **Endpoints:**
  - `POST /auth/google` signs in.
  - `POST /auth/logout` deletes the session row and clears the cookie. It is idempotent.
  - `GET /me` returns the signed-in user.
  - `DELETE /me` deletes the account; subscriptions, alert history, and sessions go with it by
    `ON DELETE CASCADE`.
- **Expiry:** expired sessions are rejected. A sign-in deletes the user's expired sessions, and
  signing in again replaces the browser's previous session.
- **One dependency for routes:** `filings_api.auth.current_user` (and `CurrentUser`) returns the
  signed-in user or raises 401. It shares the route's database session, and tests replace it
  with `app.dependency_overrides`.

### Keeping per-user data out of shared caches

- **Per-user requests bypass the cache.** The response cache never stores or serves requests
  that carry the session cookie or an `Authorization` header.
- **Some responses are never stored:** those that set a cookie, or say Cache-Control `private`
  or `no-store`.
- **Private paths** (`/auth/*`, `/me`, `/subscriptions`) are never cached and are sent with
  `Cache-Control: no-store`, so browsers and proxies don't keep them either.

### CORS and CSRF

- **CORS** allows only the configured website origins (`API_CORS_ORIGINS`) with credentials.
  The settings reject `*`. CORS is the outermost middleware:
  - It answers preflights without spending the client's rate limit.
  - Its headers reach 429 and 403 responses, which the website could not read otherwise.
  - It adds its Origin-dependent headers after the cache, so a cached response never carries
    another site's `Access-Control-Allow-Origin`.
- **CSRF, in three layers:**
  - `SameSite=Lax` already keeps the cookie off cross-site POSTs.
  - POST, PUT, PATCH, and DELETE requests whose `Origin` header names a site other than the
    website or the API itself get a 403. Browsers always send `Origin` on such cross-origin
    requests; non-browser clients aren't affected.
  - Request bodies must be JSON (415 otherwise). HTML forms can only send form or text bodies,
    and cross-origin JSON requires a preflight that only allowed origins pass.
- **Request bodies are capped at 64 KiB** (413 otherwise; added 2026-10-22), before the app
  reads them. The largest real body, a batch of alerts, is a few KB; without a cap, one
  unauthenticated request as large as Cloudflare passes (100 MB) would be held in memory and
  parsed, enough to get the API pod killed. A chunked body is counted as it arrives.

### Subscriptions

A unique constraint on `subscriptions (user_id, cik, channel, target)` stops one user
subscribing the same target twice, which would double every alert. It was added in the same
migration as `sessions`.

## Alternatives considered

- **A stateless signed session cookie (a JWT we issue).** No database lookup per request, but a
  session can't be revoked before it expires: sign-out, account deletion, and a stolen cookie
  would all stay valid. One indexed lookup in Postgres is cheap at this scale.
- **Redis for sessions.** Fast, but Redis here is a cache that fails open and can be flushed or
  evicted. Sessions must survive that, and Postgres gives `ON DELETE CASCADE` with the user.
- **google-auth (Google's library).** It does the same verification, but fetches Google's keys
  through requests or urllib3, a second HTTP stack the project avoids (ADR-0005). PyJWT plus a small httpx2 key
  fetcher keeps one HTTP client and makes the key source easy to replace in tests.
- **Authlib.** A full OAuth/OIDC client and server toolkit. It is built for redirect flows,
  which a static site can't run; verifying one ID token doesn't need it.
- **Bearer tokens in localStorage.** No CSRF exposure, since nothing is sent automatically. But
  any script on the page (an XSS bug, a compromised dependency) could read the token and use it
  elsewhere. An HttpOnly cookie can't be read by scripts, and CSRF is handled above.
- **The redirect ("authorization code") flow.** It needs a client secret and a callback handler,
  meaning more moving parts and a secret to protect, for the same result: a verified Google
  identity.

## Consequences

- **Production domains (deferred to Phase 7).** Browsers only send a `SameSite=Lax` cookie to a
  site that shares a registrable domain with the page, so the website and the API must sit on
  the same domain, e.g. `app.example.com` (a custom domain on Cloudflare Pages) and
  `api.example.com` (the tunnel). The default `*.pages.dev` address won't work: `pages.dev` is on
  the public suffix list, so it counts as a different site. Locally, use `localhost` for both
  the website and the API; `127.0.0.1` counts as a different site. (The Terraform code puts them
  on `app.<domain>` and `api.<domain>`; ADR-0018.)
- **One database query per authenticated request,** for the session lookup by its unique hash.
- **Sessions last a fixed 7 days** from sign-in; there is no sliding renewal yet. Expired rows
  of users who never return are not swept; a periodic cleanup can come later if the table grows.
- **Remaining risk.** A stolen ID token can be replayed to sign in until it expires (about an
  hour). Stealing one needs script access to our page, which would be a bigger problem anyway. A
  sign-in `nonce` would close this; it can be added if needed.
- **Google outage.** Sign-in keeps working on the last keys fetched (until Google signs with a
  newer one), and nothing else depends on Google: existing sessions never touch it. An API that
  starts during an outage can't verify sign-ins (503) until a fetch succeeds.

## Tools in plain language

- **OIDC ID token:** OpenID Connect is the standard way to "sign in with" another account. The
  ID token is the proof: a short-lived JWT, signed by Google, saying which Google account
  signed in, for which app, and when.
- **JWT:** a JSON Web Token: JSON claims plus a signature, as three base64 segments joined by
  dots. Anyone can read it; only the key holder can produce a valid signature.
- **JWKS:** a JSON Web Key Set, a published list of public keys. Google publishes the keys its ID
  tokens are signed with at a well-known URL, and rotates them regularly.
- **PyJWT:** a Python library that checks a JWT's signature and standard claims (expiry,
  audience, issuer). Its `crypto` extra adds RSA support through the `cryptography` package.
- **CORS:** Cross-Origin Resource Sharing. Browsers stop a page on one site from reading another
  site's responses unless that site allows it; for credentialed requests it must name the exact
  origin.
- **CSRF:** cross-site request forgery: a page on another site making the browser send a
  request that carries the user's cookie.
