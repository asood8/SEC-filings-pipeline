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
    expect(csp).not.toContain('unsafe-inline')
    expect(csp).not.toContain('unsafe-eval')
  })

  it('writes a Cloudflare Pages _headers file for every path', () => {
    const file = pagesHeaders('http://localhost:8000')
    const [path, ...lines] = file.trimEnd().split('\n')
    expect(path).toBe('/*')
    expect(lines.every((line) => /^ {2}[A-Za-z-]+: \S/.test(line))).toBe(true)
    expect(file).toContain('  X-Content-Type-Options: nosniff\n')
    expect(file).toContain('Content-Security-Policy-Report-Only:')
  })
})
