/** The website's response headers on Cloudflare Pages, written to dist/_headers at build time
 * (see vite.config.ts). Pages applies them to every file it serves. */

const GOOGLE_SIGN_IN = 'https://accounts.google.com/gsi/'

/** The Content-Security-Policy: only this site, the API, and Google's sign-in button may load or
 * be called. Enforced since 2026-11-18, after every page of the live site showed no violation
 * under the report-only header except Cloudflare's analytics beacon, which stays blocked. */
export function contentSecurityPolicy(apiBaseUrl: string): string {
  const api = new URL(apiBaseUrl).origin
  return [
    "default-src 'self'",
    `script-src 'self' ${GOOGLE_SIGN_IN}client`,
    // Google's sign-in script puts its button's CSS in a <style> element of its own: without
    // 'unsafe-inline' the button renders as a page-sized logo (checked 2026-11-14 with the policy
    // enforced). Its content changes with Google's releases, so a hash would break the button
    // without warning. Inline styles can't run code; scripts stay limited to these two sources.
    `style-src 'self' 'unsafe-inline' ${GOOGLE_SIGN_IN}style`,
    `frame-src ${GOOGLE_SIGN_IN}`,
    `connect-src 'self' ${api} ${GOOGLE_SIGN_IN}`,
    "img-src 'self' data: https://*.googleusercontent.com",
    "font-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
  ].join('; ')
}

export function pagesHeaders(apiBaseUrl: string): string {
  const headers = {
    'Content-Security-Policy': contentSecurityPolicy(apiBaseUrl),
    // HTTPS only, for two years. Browsers already force it on every .app domain (the TLD is on
    // their HSTS preload list); this covers clients that don't ship that list.
    'Strict-Transport-Security': 'max-age=63072000; includeSubDomains',
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
    'Referrer-Policy': 'strict-origin-when-cross-origin',
    'Permissions-Policy':
      'camera=(), microphone=(), geolocation=(), payment=()',
    // Google's sign-in popup must be able to report back to this window.
    'Cross-Origin-Opener-Policy': 'same-origin-allow-popups',
  }
  const lines = Object.entries(headers).map(
    ([name, value]) => `  ${name}: ${value}`,
  )
  return ['/*', ...lines, ''].join('\n')
}
