import { describe, expect, it } from 'vitest'
import { formatDate, formLabel } from './format'

describe('format', () => {
  it('formats date-only values without shifting the day', () => {
    expect(formatDate('2026-09-28')).toBe('Sep 28, 2026')
  })

  it('names Form 4 filings', () => {
    expect(formLabel('4')).toBe('Form 4')
    expect(formLabel('8-K')).toBe('8-K')
  })
})
