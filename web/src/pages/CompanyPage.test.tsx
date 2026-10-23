import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { API_BASE_URL } from '../api/client'
import { BERKSHIRE, BUFFETT, COOK, filing, insiderActivity } from '../test/data'
import { mockApi, renderRoute } from '../test/render'
import { rowText } from '../test/table'

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
      '/calls': () => ({ since: '2026-10-22', until: '2027-02-19', items: [] }),
    })
    renderRoute('/companies/AAPL')

    expect(
      await screen.findByRole('heading', { name: 'Apple Inc.' }),
    ).toBeInTheDocument()
    expect(document.title).toBe('Apple Inc. (AAPL) · SEC Filings')
    expect(
      screen.getByRole('link', {
        name: 'Atom feed of AAPL filings (opens in a new tab)',
      }),
    ).toHaveAttribute('href', `${API_BASE_URL}/feeds/filings.atom?cik=320193`)
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
    expect(screen.queryByText('Next call:')).not.toBeInTheDocument() // none announced
  })

  it('shows the next announced call with its calendar feed', async () => {
    const requests = mockApi({
      '/companies/AAPL': apple,
      '/companies/AAPL/insider-activity': () => insiderActivity(),
      '/filings': () => ({ items: [], next_cursor: null }),
      '/calls': () => ({
        since: '2026-10-22',
        until: '2027-02-19',
        items: [
          {
            kind: 'Earnings call',
            day: '2026-10-29',
            starts_at: '2026-10-29T17:00:00-04:00',
            webcast_url: 'https://investor.apple.com',
            ticker: 'AAPL',
            company_name: 'Apple Inc.',
            accession_no: '0000320193-26-000050',
          },
        ],
      }),
    })
    renderRoute('/companies/AAPL')

    const line = (await screen.findByText('Next call:')).closest('p')!
    expect(line).toHaveTextContent(
      'Next call: Earnings call, Oct 29, 2026, 5:00 PM ET · Webcast',
    )
    expect(
      within(line).getByRole('link', { name: 'Announcement' }),
    ).toHaveAttribute('href', '/filings/0000320193-26-000050')
    expect(
      within(line).getByRole('link', {
        name: 'Calendar feed of AAPL calls (opens in a new tab)',
      }),
    ).toHaveAttribute('href', `${API_BASE_URL}/calls.ics?ticker=AAPL`)
    const asked = requests.find((url) => url.pathname === '/calls')!
    expect(asked.searchParams.get('ticker')).toBe('AAPL')
    expect(asked.searchParams.get('days')).toBe('120')
  })

  it('names an unknown company in the title while it loads, then says not found', async () => {
    let answer: (response: Response) => void = () => {}
    mockApi({
      '/companies/ZZZZ': () =>
        new Promise<Response>((resolve) => (answer = resolve)),
    })
    renderRoute('/companies/zzzz')

    await waitFor(() => expect(document.title).toBe('ZZZZ · SEC Filings'))
    answer(Response.json({ detail: 'Not Found' }, { status: 404 }))
    expect(
      await screen.findByRole('heading', { name: 'Not found' }),
    ).toBeInTheDocument()
    expect(document.title).toBe('Company not found · SEC Filings')
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
              reporting_owners: [COOK],
              bought: { shares: 0, value: 0, unpriced_shares: 0 },
              sold: { shares: 1600.5, value: 551000, unpriced_shares: 500.5 },
              filings: 2,
              latest_date: '2026-10-05',
            },
            {
              reporting_owners: [BERKSHIRE, BUFFETT],
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
    expect(rowText(cook)).toEqual([
      'Timothy D Cook (Executive Chair, Director)',
      '—',
      '$551,000* 1,600.5 shares',
      '2',
      'Oct 5, 2026',
    ])
    expect(rowText(berkshire)).toEqual([
      'Berkshire Hathaway Inc (10% owner); Warren E Buffett (10% owner)',
      '$144,150 300 shares',
      '—',
      '1',
      'Oct 3, 2026',
    ])
    // Readable names, with the names as filed on hover.
    expect(within(cook).getByText('Timothy D Cook')).toHaveAttribute(
      'title',
      'As filed: COOK TIMOTHY D',
    )
    // The label shows the title as filed, so there's nothing to add on hover.
    expect(
      within(cook).getByText('(Executive Chair, Director)'),
    ).not.toHaveAttribute('title')
    expect(rowText(total).slice(0, 4)).toEqual([
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

  it('says how much of the selling ran under 10b5-1 plans', async () => {
    mockApi({
      '/companies/AAPL': apple,
      '/companies/AAPL/insider-activity': () =>
        insiderActivity({
          sold: { shares: 1600, value: 800_000, unpriced_shares: 0 },
          sold_planned: { shares: 1500, value: 750_000, unpriced_shares: 0 },
          filings: 2,
          owners: [
            {
              reporting_owners: [COOK],
              bought: { shares: 0, value: 0, unpriced_shares: 0 },
              sold: { shares: 1600, value: 800_000, unpriced_shares: 0 },
              filings: 2,
              latest_date: '2026-10-02',
            },
          ],
        }),
      '/filings': () => ({ items: [], next_cursor: null }),
    })
    renderRoute('/companies/AAPL')
    expect(
      await screen.findByText(
        'Under 10b5-1 plans, set up in advance: 94% of the shares sold.',
      ),
    ).toBeInTheDocument()
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
