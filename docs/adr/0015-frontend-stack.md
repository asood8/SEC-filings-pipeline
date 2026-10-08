# ADR-0015: Frontend stack: Vite, React, and TypeScript

- **Status:** Accepted
- **Date:** 2026-10-06

## Context

Phase 5 adds the website: the latest filings, search, company pages, filing detail (summary plus
a link to the original on sec.gov), and subscription management. The brief puts it on Cloudflare
Pages as static files, which is free, and leaves the framework open: Next.js or something lighter.

What shapes the choice:

- **Every page is driven by live API data.** New filings arrive all day, so filing and company
  pages can't be generated ahead of time. They are rendered in the browser from API calls either
  way.
- **No server.** Static hosting means no server-side rendering and no API routes in the frontend.
  The FastAPI service is the only backend.
- **The API already defines the contract** as an OpenAPI schema, and it will keep changing as
  phases add endpoints.
- **Every component must earn its place**, and the site is small: about six pages.

## Decision

We will build the website as a single-page app with **Vite, React, and TypeScript** (strict) in
`web/`, deployed to Cloudflare Pages as static files. The user chose this on 2026-10-06.

- **Routing:** React Router. Anything worth sharing lives in the URL (feed filter, search terms,
  search mode), so every view has a link. `public/_redirects` tells Pages to serve `index.html`
  for every path, so deep links work.
- **Server data:** TanStack Query handles caching, loading and error states, retries (network and
  5xx errors only, never 4xx or 429), and cursor pagination ("Load more").
- **Typed API client:** TypeScript types are generated from the API's OpenAPI schema
  (openapi-typescript), and requests go through a typed client (openapi-fetch). An API change that
  breaks the website becomes a compile error. CI regenerates the types and fails if the committed
  copy is stale (`make web-types`).
- **Styling:** plain CSS, with CSS modules for component styles and CSS variables for a light and
  a dark theme. No UI framework or utility-CSS tool.
- **Quality:** Vitest and Testing Library render whole pages against a stubbed `fetch`. oxlint
  lints and Prettier formats. CI runs lint, type-check, tests, and a production build in its own
  job.
- **Runtime:** Node.js 24 LTS (`web/.nvmrc`), used only to build. Production serves plain files.

## Alternatives considered

- **Next.js with static export.** Its main strengths are server-side rendering, server
  components, and API routes, and a static export uses none of them. Dynamic routes in an export
  must be listed at build time, which doesn't work for filings that arrive after the build, so
  filing pages would need workarounds. It's more recognizable on a CV, but it doesn't earn its
  extra complexity here.
- **Astro.** Excellent for content sites, but this site's content is live API data, so most pages
  would be client-side islands anyway.
- **SvelteKit.** Comparable fit, smaller ecosystem; React is the more transferable skill.
- **Fetching with `useEffect` instead of TanStack Query.** That means rewriting caching, retries,
  deduplication, and pagination state by hand.
- **Hand-written API types.** They drift from the API silently. Generated types can't.
- **Tailwind or a component library.** Another tool and build step for a handful of pages. Plain
  CSS with variables covers it.

## Consequences

- **Small and fast to build:** the production bundle is about 116 KB gzipped, and builds take
  under a second.
- **Limited SEO:** pages render in the browser, so search engines see less content than with
  server rendering. That's acceptable for this project. A static "latest filings" snapshot could
  be generated at build time later if it ever matters.
- **Version pins to watch:** openapi-typescript 7 declares support for TypeScript 5 only. An npm
  `overrides` entry lets it use the project's TypeScript 6, and type generation works. Remove the
  override once it supports 6 or later. TypeScript 7 (the native compiler) is out, but the Vite
  template still pins 6.0. Upgrade once the tooling supports it.
- **Sign-in on the website:** Google's sign-in button loads a script from `accounts.google.com`
  on the account page. The session itself is the API's HttpOnly cookie (ADR-0013), so the
  website never handles tokens.
- **Security headers:** the build writes Cloudflare Pages' `_headers` file
  (`web/src/lib/securityHeaders.ts`): nosniff, no framing, a referrer policy, a Permissions-Policy,
  a Cross-Origin-Opener-Policy that lets Google's sign-in popup report back, and a
  Content-Security-Policy allowing only the site, the API, and Google's sign-in. (The API sends
  its own; ADR-0011.) The policy
  ships report-only, since Google's sign-in can only be checked against it on the live site; once
  the browser console shows no violations there, rename the header to enforce it (runbook).
- **Local development:** `make web` serves the site on port 3000, the origin the API's CORS
  settings and the Google OAuth client allow.

## Tools in plain language

- **Vite:** a development server that reloads the page instantly as code changes, and a bundler
  that turns the source into a few optimized static files for production.
- **Single-page app:** the browser loads one HTML page once, and JavaScript draws each view and
  changes the URL without reloading the page.
- **React Router:** maps URLs such as `/filings/<accession number>` to the page component that
  shows them.
- **TanStack Query:** fetches API data and remembers it, so going back to a page is instant and a
  failed request is retried. It also tracks loading and error states for each request.
- **openapi-typescript and openapi-fetch:** the first reads the API's machine-readable
  description (OpenAPI) and writes matching TypeScript types; the second makes requests checked
  against those types, so a wrong path or field name fails to compile.
- **Vitest and Testing Library:** a test runner, plus helpers that render a page the way a user
  sees it and find elements by their visible text or role.
- **oxlint:** a very fast linter written in Rust (the frontend's counterpart to ruff). Vite's
  template uses it by default.
