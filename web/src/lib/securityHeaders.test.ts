import { describe, expect, it } from 'vitest'
import { contentSecurityPolicy, pagesHeaders } from './securityHeaders'

describe('security headers', () => {
  it('allows only this site, the API, and Google sign-in', () => {
    const csp = contentSecurityPolicy('https://api.example.com/v1')
    expect(csp).toContain(
      "connect-src 'self' https://api.example.com https://accounts.google.com/gsi/",
    )
    expect(csp).toContain(
      "script-src 'self' https://accounts.google.com/gsi/client",
    )
    expect(csp).toContain("frame-ancestors 'none'")
    // Inline styles only, for Google's sign-in button: never inline or eval'd scripts.
    const directives = csp.split('; ')
    expect(directives.filter((d) => d.includes("'unsafe-inline'"))).toEqual([
      "style-src 'self' 'unsafe-inline' https://accounts.google.com/gsi/style",
    ])
    expect(csp).not.toContain('unsafe-eval')
  })

  it('writes a Cloudflare Pages _headers file for every path', () => {
    const file = pagesHeaders('http://localhost:8000')
    const [path, ...lines] = file.trimEnd().split('\n')
    expect(path).toBe('/*')
    expect(lines.every((line) => /^ {2}[A-Za-z-]+: \S/.test(line))).toBe(true)
    expect(file).toContain('  X-Content-Type-Options: nosniff\n')
    // Enforced, not report-only.
    expect(file).toContain('  Content-Security-Policy: default-src ')
    expect(file).not.toContain('Report-Only')
    expect(file).toContain(
      '  Strict-Transport-Security: max-age=63072000; includeSubDomains\n',
    )
  })
})
