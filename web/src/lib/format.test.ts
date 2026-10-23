import { describe, expect, it } from 'vitest'
import {
  isoDateDaysAgo,
  formatDate,
  formatMoney,
  formatPrice,
  formatShares,
  formLabel,
} from './format'

describe('format', () => {
  it('counts days back from the local date, across month ends', () => {
    const now = new Date(2026, 2, 1, 23, 30) // March 1, late evening
    expect(isoDateDaysAgo(0, now)).toBe('2026-03-01')
    expect(isoDateDaysAgo(1, now)).toBe('2026-02-28')
    expect(isoDateDaysAgo(365, now)).toBe('2025-03-01')
  })

  it('formats date-only values without shifting the day', () => {
    expect(formatDate('2026-09-28')).toBe('Sep 28, 2026')
  })

  it('formats share counts, prices, and amounts', () => {
    expect(formatShares(1218429)).toBe('1,218,429')
    expect(formatShares(2605.75)).toBe('2,605.75')
    expect(formatPrice(77.6)).toBe('$77.60')
    expect(formatPrice(20.107)).toBe('$20.107')
    expect(formatPrice(0)).toBe('$0.00')
    expect(formatMoney(28.5)).toBe('$28.50')
    expect(formatMoney(55872)).toBe('$55,872')
    expect(formatMoney(595294961.61)).toBe('$595,294,962')
  })

  it('names Form 4 filings', () => {
    expect(formLabel('4')).toBe('Form 4')
    expect(formLabel('8-K')).toBe('8-K')
  })
})
