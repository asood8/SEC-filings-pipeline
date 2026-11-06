import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { CompanyRequest, User } from '../api/client'
import { mockApi, renderRoute } from '../test/render'

// Stands in for Google's script: renders a button that "signs in" with a fixed credential.
const google = vi.hoisted(() => ({
  accounts: {
    id: {
      callback: undefined as ((r: { credential: string }) => void) | undefined,
      initialize(config: { callback: (r: { credential: string }) => void }) {
        this.callback = config.callback
      },
      renderButton(parent: HTMLElement) {
        const button = document.createElement('button')
        button.textContent = 'Sign in with Google'
        button.onclick = () =>
          google.accounts.id.callback?.({ credential: 'id-token' })
        parent.replaceChildren(button)
      },
      disableAutoSelect: vi.fn(),
    },
  },
}))
vi.mock('../lib/googleSignIn', () => ({
  GOOGLE_CLIENT_ID: 'test-client.apps.googleusercontent.com',
  loadGoogleSignIn: () => Promise.resolve(google),
}))

const USER: User = {
  id: 1,
  email: 'ada@example.com',
  created_at: '2026-10-06T00:00:00Z',
  is_admin: false,
}
const UBER = { cik: 1543151, ticker: 'UBER', name: 'Uber Technologies, Inc' }
const NOW = { cik: 1373715, ticker: 'NOW', name: 'ServiceNow, Inc.' }
const ASKED: CompanyRequest = {
  id: 3,
  company: NOW,
  note: null,
  status: 'open',
  created_at: '2026-11-02T12:00:00Z',
  decided_at: null,
}

describe('SuggestCompanyPage', () => {
  it('asks signed-out visitors to sign in first', async () => {
    let signedIn = false
    mockApi({
      '/me': () =>
        signedIn
          ? USER
          : Response.json({ detail: 'not signed in' }, { status: 401 }),
      'POST /auth/google': () => {
        signedIn = true
        return USER
      },
      'GET /company-requests': () => [],
    })
    renderRoute('/suggest')

    await userEvent.click(
      await screen.findByRole('button', { name: 'Sign in with Google' }),
    )
    expect(await screen.findByLabelText('Ticker')).toBeInTheDocument()
    expect(
      screen.getByRole('heading', { level: 1, name: 'Suggest a company' }),
    ).toHaveFocus()
    expect(document.title).toBe('Suggest a company · SEC Filings')
    expect(screen.getByText('None yet.')).toBeInTheDocument()
  })

  it('suggests a company with a note and lists it as waiting', async () => {
    let mine = [ASKED]
    let sent: unknown
    mockApi({
      '/me': () => USER,
      'GET /company-requests': () => mine,
      'POST /company-requests': async (_url, request) => {
        sent = await request.json()
        const created: CompanyRequest = {
          ...ASKED,
          id: 4,
          company: UBER,
          note: 'Rides and delivery',
          created_at: '2026-11-06T12:00:00Z',
        }
        mine = [created, ...mine]
        return Response.json(created, { status: 201 })
      },
    })
    renderRoute('/suggest?ticker=UBER')

    const ticker = await screen.findByLabelText('Ticker')
    expect(ticker).toHaveValue('UBER') // from the Companies page's link
    await userEvent.type(
      screen.getByLabelText('Why follow it? (optional)'),
      '  Rides and delivery ',
    )
    await userEvent.click(screen.getByRole('button', { name: 'Suggest' }))

    expect(
      await screen.findByText(
        'Thanks: Uber Technologies, Inc (UBER) is suggested.',
      ),
    ).toHaveAttribute('role', 'status')
    expect(sent).toEqual({ ticker: 'UBER', note: 'Rides and delivery' })
    expect(ticker).toHaveValue('') // reset for the next one
    const list = screen.getByRole('list')
    const items = await within(list).findAllByRole('listitem')
    expect(items.map((item) => item.textContent)).toEqual([
      'UBER Uber Technologies, IncSuggested Nov 6, 2026: waiting for a decision.',
      'NOW ServiceNow, Inc.Suggested Nov 2, 2026: waiting for a decision.',
    ])
    // Not an admin: no link to the review page.
    expect(
      screen.queryByRole('link', { name: /Review everyone/ }),
    ).not.toBeInTheDocument()
  })

  it("shows why the API refused, and each suggestion's decision", async () => {
    mockApi({
      '/me': () => ({ ...USER, is_admin: true }),
      'GET /company-requests': () => [
        {
          ...ASKED,
          company: UBER,
          status: 'added',
          decided_at: '2026-11-05T12:00:00Z',
        },
        { ...ASKED, status: 'declined', decided_at: '2026-11-04T12:00:00Z' },
      ],
      'POST /company-requests': () =>
        Response.json({ detail: 'AAPL is already followed' }, { status: 409 }),
    })
    renderRoute('/suggest')

    await userEvent.type(await screen.findByLabelText('Ticker'), 'aapl')
    await userEvent.click(screen.getByRole('button', { name: 'Suggest' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'AAPL is already followed',
    )

    const [added, declined] = await screen.findAllByRole('listitem')
    expect(added).toHaveTextContent('Added Nov 5, 2026: see its filings.')
    expect(
      within(added).getByRole('link', { name: 'see its filings' }),
    ).toHaveAttribute('href', '/companies/UBER')
    expect(declined).toHaveTextContent(
      'Suggested Nov 2, 2026: not added (Nov 4, 2026).',
    )
    expect(
      screen.getByRole('link', { name: "Review everyone's suggestions" }),
    ).toHaveAttribute('href', '/admin/suggestions')
  })
})
