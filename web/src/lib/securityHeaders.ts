/** The website's response headers on Cloudflare Pages, written to dist/_headers at build time
 * (see vite.config.ts). Pages applies them to every file it serves. */

const GOOGLE_SIGN_IN = 'https://accounts.google.com/gsi/'

/** The Content-Security-Policy: only this site, the API, and Google's sign-in button may load or
 * be called. It ships report-only (violations show in the browser console without blocking
 * anything) until it has been checked against the live site; then rename the header. */
export function contentSecurityPolicy(apiBaseUrl: string): string {
  const api = new URL(apiBaseUrl).origin
  return [
    "default-src 'self'",
    `script-src 'self' ${GOOGLE_SIGN_IN}client`,
    `style-src 'self' ${GOOGLE_SIGN_IN}style`,
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
    'Content-Security-Policy-Report-Only': contentSecurityPolicy(apiBaseUrl),
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
