import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { filing, insiderActivity } from '../test/data'
import { mockApi, renderRoute } from '../test/render'

const apple = () => ({
  cik: 320193,
  ticker: 'AAPL',
  name: 'Apple Inc.',
  exchange: 'Nasdaq',
  on_watchlist: true,
  recent_filings: [],
})

describe('CompanyPage', () => {
  it("lists the company's filings", async () => {
    const requests = mockApi({
      '/companies/AAPL': apple,
      '/companies/AAPL/insider-activity': () => insiderActivity(),
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
    expect(
      await screen.findByText(
        'No insider purchases or sales of AAPL stock reported in the last 90 days.',
      ),
    ).toBeInTheDocument()
  })

  it('shows insider buying and selling by owner, with totals', async () => {
    const requests = mockApi({
      '/companies/AAPL': apple,
      '/companies/AAPL/insider-activity': () =>
        insiderActivity({
          bought: { shares: 300, value: 144150, unpriced_shares: 0 },
          sold: { shares: 1600.5, value: 551000, unpriced_shares: 500.5 },
          filings: 3,
          owners: [
            {
              reporting_owners: ['COOK TIMOTHY D'],
              bought: { shares: 0, value: 0, unpriced_shares: 0 },
              sold: { shares: 1600.5, value: 551000, unpriced_shares: 500.5 },
              filings: 2,
              latest_date: '2026-10-05',
            },
            {
              reporting_owners: ['BERKSHIRE HATHAWAY INC', 'BUFFETT WARREN E'],
              bought: { shares: 300, value: 144150, unpriced_shares: 0 },
              sold: { shares: 0, value: 0, unpriced_shares: 0 },
              filings: 1,
              latest_date: '2026-10-03',
            },
          ],
        }),
      '/filings': () => ({ items: [], next_cursor: null }),
    })
    renderRoute('/companies/AAPL')

    const table = await screen.findByRole('table', {
      name: /Insider purchases and sales by owner/,
    })
    const [, cook, berkshire, total] = within(table).getAllByRole('row')
    const cells = (row: HTMLElement) =>
      within(row)
        .getAllByRole('cell')
        .map((cell) => cell.textContent)
    expect(cells(cook)).toEqual([
      'COOK TIMOTHY D',
      '—',
      '$551,000* 1,600.5 shares',
      '2',
      'Oct 5, 2026',
    ])
    expect(cells(berkshire)).toEqual([
      'BERKSHIRE HATHAWAY INC; BUFFETT WARREN E',
      '$144,150 300 shares',
      '—',
      '1',
      'Oct 3, 2026',
    ])
    expect(cells(total).slice(0, 4)).toEqual([
      'All insiders',
      '$144,150 300 shares',
      '$551,000* 1,600.5 shares',
      '3',
    ])
    expect(screen.getByText(/since Jul 10, 2026/)).toBeInTheDocument()
    expect(
      screen.getByText(/price the filing gives only in a footnote/),
    ).toBeInTheDocument()
    expect(
      requests
        .find((url) => url.pathname === '/companies/AAPL/insider-activity')
        ?.searchParams.get('days'),
    ).toBe('90')
  })

  it('says when insider trades are loading', async () => {
    mockApi({
      '/companies/AAPL': apple,
      '/companies/AAPL/insider-activity': () => new Promise(() => {}),
      '/filings': () => ({ items: [], next_cursor: null }),
    })
    renderRoute('/companies/AAPL')

    expect(
      await screen.findByText('Loading insider trades…'),
    ).toBeInTheDocument()
  })
})
