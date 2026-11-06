import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import type { CompanySuggestion, User } from '../api/client'
import { mockApi, renderRoute } from '../test/render'

const ADMIN: User = {
  id: 1,
  email: 'owner@example.com',
  created_at: '2026-10-06T00:00:00Z',
  is_admin: true,
}
const UBER: CompanySuggestion = {
  company: { cik: 1543151, ticker: 'UBER', name: 'Uber Technologies, Inc' },
  on_watchlist: true,
  requests: 2,
  first_requested_at: '2026-11-01T12:00:00Z',
  last_requested_at: '2026-11-05T12:00:00Z',
  status: 'open',
  notes: [
    {
      note: 'Rides and delivery',
      by: 'a***@example.com',
      at: '2026-11-05T12:00:00Z',
    },
  ],
}
const NOW: CompanySuggestion = {
  ...UBER,
  company: { cik: 1373715, ticker: 'NOW', name: 'ServiceNow, Inc.' },
  on_watchlist: false,
  requests: 1,
  notes: [],
}

describe('CompanySuggestionsPage', () => {
  it('is for the admin only', async () => {
    mockApi({ '/me': () => ({ ...ADMIN, is_admin: false }) })
    renderRoute('/admin/suggestions')
    expect(
      await screen.findByText(/Only the site's admin can review suggestions/),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('link', { name: 'Suggest a company' }),
    ).toHaveAttribute('href', '/suggest')
  })

  it('lists suggestions by company and records a decision', async () => {
    let open = [UBER, NOW]
    let decided: unknown
    mockApi({
      '/me': () => ADMIN,
      'GET /admin/company-requests': (url) =>
        url.searchParams.get('status') === 'open' ? open : [],
      'PATCH /admin/company-requests/UBER': async (_url, request) => {
        decided = await request.json()
        open = [NOW]
        return { ...UBER, status: 'added' }
      },
      'PATCH /admin/company-requests/NOW': () =>
        Response.json(
          { detail: "NOW isn't on the watchlist yet: add it there first" },
          { status: 409 },
        ),
    })
    renderRoute('/admin/suggestions')

    expect(
      await screen.findByRole('heading', {
        name: 'Waiting for a decision (2)',
      }),
    ).toBeInTheDocument()
    expect(document.title).toBe('Company suggestions · SEC Filings')
    const list = screen.getByRole('list', {
      name: 'Waiting for a decision (2)',
    })
    const [uber, now] = [...list.children] as HTMLElement[]
    expect(uber).toHaveTextContent('2 people asked, last on Nov 5, 2026.')
    expect(
      within(uber).getByRole('link', { name: 'On the watchlist' }),
    ).toHaveAttribute('href', '/companies/UBER')
    expect(
      within(uber).getByRole('list', { name: 'Notes on UBER' }),
    ).toHaveTextContent('“Rides and delivery”a***@example.com, Nov 5, 2026')
    expect(now).toHaveTextContent(
      '1 person asked, last on Nov 5, 2026. Not on the watchlist.',
    )

    // Added before it's on the watchlist: the API says why not, beside that company.
    await userEvent.click(
      within(now).getByRole('button', { name: 'Mark NOW added' }),
    )
    expect(await within(now).findByRole('alert')).toHaveTextContent(
      "NOW isn't on the watchlist yet: add it there first",
    )

    await userEvent.click(
      within(uber).getByRole('button', { name: 'Mark UBER added' }),
    )
    expect(await screen.findByText('UBER marked added.')).toHaveAttribute(
      'role',
      'status',
    )
    expect(decided).toEqual({ status: 'added' })
    const heading = await screen.findByRole('heading', {
      name: 'Waiting for a decision (1)',
    })
    expect(heading).toHaveFocus() // the button left with UBER

    await userEvent.selectOptions(screen.getByLabelText('Show'), 'Added')
    expect(
      await screen.findByRole('heading', { name: 'Added (0)' }),
    ).toBeInTheDocument()
    expect(screen.getByText('None.')).toBeInTheDocument()
  })
})
