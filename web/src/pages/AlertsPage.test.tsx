import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Subscription } from '../api/client'
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
    expect(credential).toBe('id-token')
    expect(screen.getByText(/…abcd/)).toBeInTheDocument()
  })

  it('adds an alert and shows API errors', async () => {
    let created: unknown
    mockApi({
      '/me': () => USER,
      'GET /subscriptions': () => subscriptions,
      'POST /subscriptions': async (_url, request) => {
        created = await request.json()
        if (subscriptions.length > 1) {
          return Response.json(
            {
              detail:
                'you already have a subscription to MSFT with this channel and target',
            },
            { status: 409 },
          )
        }
        subscriptions = [...subscriptions, { ...APPLE_ALERT, id: 8 }]
        return Response.json(subscriptions[1], { status: 201 })
      },
      '/companies': () => WATCHLIST,
    })
    renderRoute('/alerts?ticker=msft')

    const form = (
      await screen.findByRole('heading', { name: 'Add an alert' })
    ).closest('form')!
    await screen.findByRole('option', { name: /MICROSOFT/ })
    expect(within(form).getByLabelText('Company')).toHaveValue('MSFT') // from the company page
    await userEvent.click(within(form).getByLabelText('Current reports (8-K)'))
    await userEvent.type(
      within(form).getByLabelText(/Webhook URL/),
      'https://discord.com/api/webhooks/1/token',
    )
    await userEvent.click(
      within(form).getByRole('button', { name: 'Add alert' }),
    )
    expect(
      await screen.findAllByRole('button', { name: /Remove the AAPL alert/ }),
    ).toHaveLength(2)
    expect(created).toEqual({
      ticker: 'MSFT',
      channel: 'discord',
      form_types: ['8-K'],
      target: 'https://discord.com/api/webhooks/1/token',
    })

    await userEvent.selectOptions(
      within(form).getByLabelText('Company'),
      'MSFT',
    )
    await userEvent.selectOptions(
      within(form).getByLabelText('Send to'),
      'email',
    )
    expect(
      within(form).getByText(/go to your account's address, ada@example.com/),
    ).toBeInTheDocument()
    await userEvent.click(
      within(form).getByRole('button', { name: 'Add alert' }),
    )
    expect(await within(form).findByRole('alert')).toHaveTextContent(
      'You already have a subscription to MSFT',
    )
    expect(created).toMatchObject({ channel: 'email', target: null })
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
      await screen.findByRole('button', { name: 'Remove the AAPL alert' }),
    )
    expect(
      await screen.findByText('No alerts yet. Add one below.'),
    ).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Sign out' }))
    expect(
      await screen.findByRole('button', { name: 'Sign in with Google' }),
    ).toBeInTheDocument()
  })
})
