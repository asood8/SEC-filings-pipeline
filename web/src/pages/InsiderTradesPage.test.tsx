import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import type { InsiderTrade } from '../api/client'
import { BERKSHIRE, BUFFETT, insiderTrade } from '../test/data'
import { mockApi, renderRoute } from '../test/render'
import { rowText } from '../test/table'

/** The API's answer for the request's side and days. */
function answer(url: URL, items: InsiderTrade[]) {
  return {
    side: url.searchParams.get('side') ?? 'buy',
    days: Number(url.searchParams.get('days') ?? 30),
    since: '2026-09-08',
    items,
  }
}

describe('InsiderTradesPage', () => {
  it('lists the largest purchases, linking to filings and companies', async () => {
    const requests = mockApi({
      '/insider-trades': (url) =>
        answer(url, [
          insiderTrade({
            accession_no: '0001193125-26-397059',
            filed_at: '2026-09-21',
            transaction_date: '2026-09-19',
            issuer: { cik: 920760, name: 'Lennar Corp', ticker: 'LEN' },
            reporting_owners: [BERKSHIRE, BUFFETT],
            side: 'buy',
            shares: 2743529,
            value: 212395151.42,
            average_price: 77.4168,
            is_10b5_1: false,
          }),
          // Not in SEC's ticker list, so no company page; priced only in a footnote.
          insiderTrade({
            accession_no: '0001083839-26-000001',
            issuer: {
              cik: 1083839,
              name: 'Example Municipal Fund',
              ticker: null,
            },
            side: 'buy',
            shares: 1000,
            value: 0,
            average_price: null,
            unpriced_shares: 1000,
            is_10b5_1: null,
          }),
        ]),
    })
    const router = renderRoute('/insiders')

    const table = await screen.findByRole('table', {
      name: 'Largest insider purchases, last 30 days',
    })
    const [, lennar, fund] = within(table).getAllByRole('row')
    expect(rowText(lennar)).toEqual([
      'LEN Lennar Corp',
      'Berkshire Hathaway Inc (10% owner); Warren E Buffett (10% owner)',
      '$212,395,151 2,743,529 shares',
      '$77.42',
      'Sep 19, 2026',
      'No',
      'Filed Sep 21, 2026 (LEN Form 4)',
    ])
    // Each row is titled by its stock, for screen readers moving across the row.
    expect(within(lennar).getByRole('rowheader')).toHaveTextContent(
      'LEN Lennar Corp',
    )
    expect(within(lennar).getByRole('link', { name: 'LEN' })).toHaveAttribute(
      'href',
      '/companies/LEN',
    )
    expect(
      // The link's name says which filing it opens, out of context too.
      within(lennar).getByRole('link', {
        name: 'Filed Sep 21, 2026 (LEN Form 4)',
      }),
    ).toHaveAttribute('href', '/filings/0001193125-26-397059')
    expect(rowText(fund)).toEqual([
      'Example Municipal Fund',
      'Timothy D Cook (Executive Chair, Director)',
      '—* 1,000 shares',
      '—',
      'Oct 2, 2026',
      'Not stated',
      'Filed Oct 5, 2026 (Example Municipal Fund Form 4)',
    ])
    expect(within(fund).getAllByRole('link')).toHaveLength(1) // the filing only
    expect(screen.getByText(/since Sep 8, 2026/)).toBeInTheDocument()
    expect(
      screen.getByText(/price the filing gives only in a footnote/),
    ).toBeInTheDocument()
    expect(screen.getByText(/Awards, option exercises/)).toBeInTheDocument()
    expect(
      screen.getByText(/10b5-1 plan is a trading plan/),
    ).toBeInTheDocument()
    expect(screen.getByLabelText('Purchases')).toBeChecked()
    expect(screen.getByLabelText('Last 30 days')).toBeChecked()
    expect(requests.at(-1)?.search).toBe('?side=buy&days=30')
    expect(router.state.location.search).toBe('')
    expect(
      screen.getByRole('link', { name: 'Insider trades' }),
    ).toHaveAttribute('aria-current', 'page')
  })

  it('keeps the choices in the URL', async () => {
    const requests = mockApi({
      '/insider-trades': (url) => answer(url, [insiderTrade()]),
    })
    const router = renderRoute('/insiders')
    await screen.findByRole('table')

    await userEvent.click(screen.getByLabelText('Sales'))
    expect(
      await screen.findByRole('table', {
        name: 'Largest insider sales, last 30 days',
      }),
    ).toBeInTheDocument()
    expect(router.state.location.search).toBe('?side=sell')
    expect(requests.at(-1)?.search).toBe('?side=sell&days=30')

    await userEvent.click(screen.getByLabelText('Last 90 days'))
    await screen.findByRole('table', {
      name: 'Largest insider sales, last 90 days',
    })
    expect(router.state.location.search).toBe('?side=sell&days=90')
    expect(requests.at(-1)?.search).toBe('?side=sell&days=90')

    await userEvent.click(screen.getByLabelText('Purchases'))
    await screen.findByRole('table', {
      name: 'Largest insider purchases, last 90 days',
    })
    expect(router.state.location.search).toBe('?days=90')
  })

  it('starts from the choices in the URL', async () => {
    const requests = mockApi({ '/insider-trades': (url) => answer(url, []) })
    renderRoute('/insiders?side=sell&days=90')
    expect(
      await screen.findByText('No insider sales reported in the last 90 days.'),
    ).toBeInTheDocument()
    expect(screen.getByLabelText('Sales')).toBeChecked()
    expect(screen.getByLabelText('Last 90 days')).toBeChecked()
    expect(requests.at(-1)?.search).toBe('?side=sell&days=90')
  })

  it('falls back to the defaults for choices it does not offer', async () => {
    const requests = mockApi({ '/insider-trades': (url) => answer(url, []) })
    renderRoute('/insiders?side=hold&days=7')
    expect(
      await screen.findByText(
        'No insider purchases reported in the last 30 days.',
      ),
    ).toBeInTheDocument()
    expect(requests.at(-1)?.search).toBe('?side=buy&days=30')
  })

  it('says when trades are loading', async () => {
    mockApi({ '/insider-trades': () => new Promise(() => {}) })
    renderRoute('/insiders')
    expect(
      await screen.findByText('Loading insider trades…'),
    ).toBeInTheDocument()
  })

  it('explains API errors', async () => {
    mockApi({
      '/insider-trades': () => new Response(null, { status: 503 }),
    })
    renderRoute('/insiders')
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The server had a problem',
    )
  })
})
