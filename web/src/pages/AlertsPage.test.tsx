import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Subscription } from '../api/client'
import { filing } from '../test/data'
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

const USER = {
  id: 1,
  email: 'ada@example.com',
  created_at: '2026-10-06T00:00:00Z',
}
const WATCHLIST = {
  items: [
    {
      cik: 320193,
      ticker: 'AAPL',
      name: 'Apple Inc.',
      exchange: 'Nasdaq',
      on_watchlist: true,
    },
    {
      cik: 789019,
      ticker: 'MSFT',
      name: 'MICROSOFT CORP',
      exchange: 'Nasdaq',
      on_watchlist: true,
    },
  ],
  next_cursor: null,
}
const APPLE_ALERT: Subscription = {
  id: 7,
  company: { cik: 320193, ticker: 'AAPL', name: 'Apple Inc.' },
  form_types: [],
  form4_trades: 'all',
  form4_min_value: null,
  form8k_items: [],
  query: null,
  channel: 'discord',
  target: 'https://discord.com/api/webhooks/123/…abcd',
  created_at: '2026-10-06T00:00:00Z',
}

describe('AlertsPage', () => {
  let subscriptions: Subscription[]
  beforeEach(() => {
    subscriptions = [APPLE_ALERT]
  })

  it('signs in with Google, then shows the alerts', async () => {
    let signedIn = false
    let credential: unknown
    mockApi({
      '/me': () =>
        signedIn
          ? USER
          : Response.json({ detail: 'not signed in' }, { status: 401 }),
      'POST /auth/google': async (_url, request) => {
        credential = ((await request.json()) as { credential: string })
          .credential
        signedIn = true
        return USER
      },
      'GET /subscriptions': () => subscriptions,
      '/companies': () => WATCHLIST,
    })
    renderRoute('/alerts')

    await userEvent.click(
      await screen.findByRole('button', { name: 'Sign in with Google' }),
    )
    expect(await screen.findByText('ada@example.com')).toBeInTheDocument()
    // The sign-in button is gone, so focus goes to the page's heading.
    expect(
      screen.getByRole('heading', { level: 1, name: 'Alerts' }),
    ).toHaveFocus()
    expect(credential).toBe('id-token')
    expect(screen.getByText(/…abcd/)).toBeInTheDocument()
  })

  it('adds several companies and destinations at once, reusing a saved webhook', async () => {
    let sent: unknown
    mockApi({
      '/me': () => USER,
      'GET /subscriptions': () => subscriptions,
      'POST /subscriptions/batch': async (_url, request) => {
        sent = await request.json()
        const created = [8, 9, 10, 11].map((id) => ({ ...APPLE_ALERT, id }))
        subscriptions = [...subscriptions, ...created]
        return Response.json(
          { created, existing: [APPLE_ALERT] },
          { status: 201 },
        )
      },
      '/companies': () => WATCHLIST,
    })
    renderRoute('/alerts?ticker=msft')

    const form = (
      await screen.findByRole('heading', { name: 'Add alerts' })
    ).closest('form')!
    const msft = await within(form).findByRole('checkbox', { name: /MSFT/ })
    expect(msft).toBeChecked() // preselected from the company page
    await userEvent.click(within(form).getByRole('checkbox', { name: /AAPL/ }))
    await userEvent.click(within(form).getByLabelText('Current reports (8-K)'))
    await userEvent.click(
      within(form).getByRole('checkbox', { name: 'Discord' }),
    )
    // The webhook already in use is offered instead of pasting it again.
    expect(within(form).getByLabelText('Discord webhook')).toHaveDisplayValue(
      'Same webhook as before (…abcd)',
    )
    expect(
      within(form).queryByLabelText('Discord webhook URL'),
    ).not.toBeInTheDocument()
    await userEvent.click(within(form).getByRole('checkbox', { name: 'Slack' }))
    await userEvent.type(
      within(form).getByLabelText('Slack webhook URL'),
      'https://hooks.slack.com/services/T1/B1/token',
    )
    await userEvent.click(
      within(form).getByRole('button', { name: 'Add 4 alerts' }),
    )

    // In a live region, so screen readers announce it.
    expect(
      (
        await within(form).findByText('Added 4 alerts. 1 already existed.')
      ).closest('[role="status"]'),
    ).not.toBeNull()
    expect(sent).toEqual({
      tickers: ['MSFT', 'AAPL'],
      form_types: ['8-K'],
      form4_trades: 'all', // every Form 4 unless a filter is chosen
      form4_min_value: null,
      form8k_items: [], // every 8-K
      query: null,
      destinations: [
        { channel: 'discord', target_from: 7 },
        {
          channel: 'slack',
          target: 'https://hooks.slack.com/services/T1/B1/token',
        },
      ],
    })
    expect(
      within(form).getByRole('checkbox', { name: /MSFT/ }),
    ).not.toBeChecked() // reset
  })

  it('adds a keyword alert on every company and lists it on its own', async () => {
    let sent: unknown
    const KEYWORD_ALERT: Subscription = {
      ...APPLE_ALERT,
      id: 12,
      company: null,
      query: 'buyback',
      channel: 'email',
      target: 'ada@example.com',
    }
    mockApi({
      '/me': () => USER,
      'GET /subscriptions': () => subscriptions,
      'POST /subscriptions/batch': async (_url, request) => {
        sent = await request.json()
        subscriptions = [...subscriptions, KEYWORD_ALERT]
        return Response.json(
          { created: [KEYWORD_ALERT], existing: [] },
          { status: 201 },
        )
      },
      '/companies': () => WATCHLIST,
    })
    renderRoute('/alerts')

    const form = (
      await screen.findByRole('heading', { name: 'Add alerts' })
    ).closest('form')!
    await userEvent.type(
      within(form).getByLabelText('Only filings mentioning (optional)'),
      'buyback',
    )
    await userEvent.click(
      within(form).getByRole('checkbox', { name: /^Email to / }),
    )
    // No company chosen: one keyword alert on every company.
    await userEvent.click(
      within(form).getByRole('button', { name: 'Add 1 alert' }),
    )
    await waitFor(() =>
      expect(sent).toMatchObject({ tickers: [], query: 'buyback' }),
    )
    const group = (await screen.findByText('Any watchlist company')).closest(
      'li',
    )!
    expect(group).toHaveTextContent('mentioning “buyback”')
    expect(
      within(group).getByRole('button', {
        name: 'Remove the “buyback” Email alert',
      }),
    ).toBeInTheDocument()
  })

  it('previews which recent filings the settings would have sent', async () => {
    const requests = mockApi({
      '/me': () => USER,
      'GET /subscriptions': () => subscriptions,
      '/companies': () => WATCHLIST,
      '/subscriptions/preview': () => ({
        since: '2026-07-24',
        matched: 12,
        capped: false,
        items: [
          filing({ accession_no: 'a', form_type: '4', headline: 'Cook sold' }),
        ],
      }),
    })
    renderRoute('/alerts')

    const form = (
      await screen.findByRole('heading', { name: 'Add alerts' })
    ).closest('form')!
    const button = within(form).getByRole('button', {
      name: 'Preview the last 90 days',
    })
    expect(button).toBeDisabled() // nothing chosen yet
    await userEvent.click(
      await within(form).findByRole('checkbox', { name: /AAPL/ }),
    )
    await userEvent.selectOptions(
      within(form).getByRole('combobox', { name: 'Insider trades (Form 4)' }),
      'Open-market purchases only',
    )
    await userEvent.click(button)

    expect(
      await within(form).findByText(
        '12 filings in the last 90 days would have sent this alert; the newest 1:',
      ),
    ).toBeInTheDocument()
    expect(
      within(form).getByRole('link', { name: 'AAPL Form 4: Cook sold' }),
    ).toHaveAttribute('href', '/filings/a')
    const sent = requests.find(
      (url) => url.pathname === '/subscriptions/preview',
    )!
    expect(sent.searchParams.getAll('tickers')).toEqual(['AAPL'])
    expect(sent.searchParams.get('form4_trades')).toBe('purchases')
    expect(sent.searchParams.get('days')).toBe('90')
    expect(sent.searchParams.has('form4_min_value')).toBe(false)

    // Changing a setting clears the preview, which no longer matches the form.
    await userEvent.click(within(form).getByRole('checkbox', { name: /MSFT/ }))
    expect(
      within(form).queryByRole('link', { name: 'AAPL Form 4: Cook sold' }),
    ).not.toBeInTheDocument()
  })

  it('shows API errors', async () => {
    mockApi({
      '/me': () => USER,
      'GET /subscriptions': () => [],
      'POST /subscriptions/batch': () =>
        Response.json(
          {
            detail: 'that would make 26 subscriptions, over the maximum of 25',
          },
          { status: 409 },
        ),
      '/companies': () => WATCHLIST,
    })
    renderRoute('/alerts')

    const form = (
      await screen.findByRole('heading', { name: 'Add alerts' })
    ).closest('form')!
    const submit = within(form).getByRole('button', {
      name: /Choose companies/,
    })
    expect(submit).toBeDisabled()
    await userEvent.click(
      await within(form).findByRole('checkbox', { name: /AAPL/ }),
    )
    await userEvent.click(
      within(form).getByRole('checkbox', { name: /Email to ada@example.com/ }),
    )
    await userEvent.click(
      within(form).getByRole('button', { name: 'Add 1 alert' }),
    )
    expect(await within(form).findByRole('alert')).toHaveTextContent(
      'over the maximum of 25',
    )
  })

  it("shows failed deliveries and edits an alert's forms", async () => {
    let patched: unknown
    subscriptions = [
      {
        ...APPLE_ALERT,
        last_delivery: {
          status: 'failed',
          accession_no: '0000320193-26-000001',
          at: '2026-10-07T14:00:00Z',
          error: 'Discord rejected the alert: 404',
        },
      },
    ]
    mockApi({
      '/me': () => USER,
      'GET /subscriptions': () => subscriptions,
      'PATCH /subscriptions/7': async (_url, request) => {
        patched = await request.json()
        subscriptions = [
          { ...subscriptions[0], ...(patched as Partial<Subscription>) },
        ]
        return subscriptions[0]
      },
      '/companies': () => WATCHLIST,
    })
    renderRoute('/alerts')

    expect(await screen.findByText(/Last alert failed/)).toHaveTextContent(
      'Discord rejected the alert: 404',
    )
    await userEvent.click(
      screen.getByRole('button', {
        name: 'Edit the forms and filters for the AAPL Discord alert',
      }),
    )
    const form = screen.getByRole('form', {
      name: 'Forms for the AAPL Discord alert',
    })
    await userEvent.click(within(form).getByLabelText('Form 4'))
    await userEvent.selectOptions(
      within(form).getByLabelText('Insider trades (Form 4)'),
      'Open-market purchases only',
    )
    await userEvent.type(
      within(form).getByLabelText(/Only when those trades total at least/),
      '100000',
    )
    await userEvent.click(
      within(form).getByLabelText('2.02 Results of operations (earnings)'),
    )
    await userEvent.click(within(form).getByRole('button', { name: 'Save' }))
    expect(await screen.findByText('(Form 4)')).toBeInTheDocument()
    expect(
      screen.getByText(
        '· Form 4: purchases only, $100,000 or more · 8-K: 2.02',
      ),
    ).toBeInTheDocument()
    expect(patched).toEqual({
      form_types: ['4'],
      form4_trades: 'purchases',
      form4_min_value: 100000,
      form8k_items: ['2.02'],
    })
    expect(
      screen.queryByRole('form', { name: 'Forms for the AAPL Discord alert' }),
    ).not.toBeInTheDocument()
    // Focus returns to the button that opened the form.
    const edit = screen.getByRole('button', {
      name: 'Edit the forms and filters for the AAPL Discord alert',
    })
    expect(edit).toHaveFocus()
    expect(edit).toHaveAttribute('aria-expanded', 'false')
  })

  it('closes the edit form with Escape, back on its button', async () => {
    mockApi({
      '/me': () => USER,
      'GET /subscriptions': () => subscriptions,
      '/companies': () => WATCHLIST,
    })
    renderRoute('/alerts')

    const edit = await screen.findByRole('button', {
      name: 'Edit the forms and filters for the AAPL Discord alert',
    })
    await userEvent.click(edit)
    expect(edit).toHaveAttribute('aria-expanded', 'true')
    await userEvent.click(screen.getByLabelText('Form 4'))
    await userEvent.keyboard('{Escape}')
    expect(
      screen.queryByRole('form', { name: 'Forms for the AAPL Discord alert' }),
    ).not.toBeInTheDocument()
    expect(edit).toHaveFocus()
  })

  it('removes an alert and signs out', async () => {
    let signedIn = true
    mockApi({
      '/me': () =>
        signedIn
          ? USER
          : Response.json({ detail: 'not signed in' }, { status: 401 }),
      'GET /subscriptions': () => subscriptions,
      'DELETE /subscriptions/7': () => {
        subscriptions = []
        return new Response(null, { status: 204 })
      },
      'POST /auth/logout': () => {
        signedIn = false
        return new Response(null, { status: 204 })
      },
      '/companies': () => WATCHLIST,
    })
    renderRoute('/alerts')

    await userEvent.click(
      await screen.findByRole('button', {
        name: 'Remove the AAPL Discord alert',
      }),
    )
    expect(
      await screen.findByText('No alerts yet. Add some below.'),
    ).toBeInTheDocument()
    // The removed alert's button went with it: focus moves to the list's heading, and screen
    // readers hear what happened.
    await waitFor(() =>
      expect(
        screen.getByRole('heading', { name: 'Your alerts' }),
      ).toHaveFocus(),
    )
    expect(screen.getByText('Removed the AAPL Discord alert.')).toBeVisible()
    expect(
      screen.getByText('Removed the AAPL Discord alert.').closest('[role]'),
    ).toHaveAttribute('role', 'status')

    await userEvent.click(screen.getByRole('button', { name: 'Sign out' }))
    expect(
      await screen.findByRole('button', { name: 'Sign in with Google' }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('heading', { level: 1, name: 'Alerts' }),
    ).toHaveFocus()
  })
})
