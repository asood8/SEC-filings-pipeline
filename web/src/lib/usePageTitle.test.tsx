import { renderHook } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { pageTitle, usePageTitle } from './usePageTitle'

describe('usePageTitle', () => {
  it("puts the page's title before the site's name", () => {
    const { rerender } = renderHook(
      ({ title }: { title?: string }) => usePageTitle(title),
      { initialProps: { title: 'Apple Inc. (AAPL)' } },
    )
    expect(document.title).toBe('Apple Inc. (AAPL) · SEC Filings')

    rerender({ title: 'Search: buyback' })
    expect(document.title).toBe('Search: buyback · SEC Filings')
  })

  it("shows the site's name alone while the page is loading", () => {
    renderHook(() => usePageTitle(undefined))
    expect(document.title).toBe('SEC Filings')
    expect(pageTitle('')).toBe('SEC Filings')
  })
})
