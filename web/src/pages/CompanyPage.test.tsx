import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { filing } from '../test/data'
import { mockApi, renderRoute } from '../test/render'

describe('CompanyPage', () => {
  it("lists the company's filings", async () => {
    const requests = mockApi({
      '/companies/AAPL': () => ({
        cik: 320193,
        ticker: 'AAPL',
        name: 'Apple Inc.',
        exchange: 'Nasdaq',
        on_watchlist: true,
        recent_filings: [],
      }),
      '/filings': () => ({ items: [filing()], next_cursor: null }),
    })
    renderRoute('/companies/AAPL')

    expect(
      await screen.findByRole('heading', { name: 'Apple Inc.' }),
    ).toBeInTheDocument()
    expect(
      await screen.findByRole('link', { name: /Kevan Parekh/ }),
    ).toBeInTheDocument()
    expect(
      requests
        .find((url) => url.pathname === '/filings')
        ?.searchParams.get('cik'),
    ).toBe('320193')
  })
})
