import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { mockApi, renderRoute } from '../test/render'

const ALERT = {
  company: { cik: 320193, ticker: 'AAPL', name: 'Apple Inc.' },
  query: null,
  target: 'a***@example.com',
}

describe('UnsubscribePage', () => {
  it('stops the email alert only when the button is pressed', async () => {
    let posted = 0
    const requests = mockApi({
      'GET /unsubscribe': () => ALERT,
      'POST /unsubscribe': () => {
        posted += 1
        return { unsubscribed: true }
      },
    })
    renderRoute('/unsubscribe?token=7.abc')

    expect(
      await screen.findByText(
        /Stop the alerts about Apple Inc\. \(AAPL\) sent to a\*\*\*@example\.com\?/,
      ),
    ).toBeInTheDocument()
    expect(document.title).toBe('Stop email alerts · Filingwire')
    expect(posted).toBe(0) // opening the link changes nothing
    await userEvent.click(
      screen.getByRole('button', { name: 'Stop these alerts' }),
    )
    expect(await screen.findByRole('status')).toHaveTextContent(
      "You won't get these emails any more.",
    )
    expect(posted).toBe(1)
    const post = requests.filter((url) => url.pathname === '/unsubscribe')[1]
    expect(post.searchParams.get('token')).toBe('7.abc')
  })

  it('says when the alert is already gone or the link is broken', async () => {
    mockApi({
      'GET /unsubscribe': (url) =>
        url.searchParams.get('token') === 'gone'
          ? Response.json({ detail: 'already unsubscribed' }, { status: 404 })
          : Response.json({ detail: 'not valid' }, { status: 400 }),
    })
    renderRoute('/unsubscribe?token=gone')
    expect(
      await screen.findByText(/This alert is already gone/),
    ).toBeInTheDocument()
  })

  it('says a link without a token is not valid', async () => {
    mockApi({})
    renderRoute('/unsubscribe')
    expect(
      await screen.findByText(/This unsubscribe link isn't valid/),
    ).toBeInTheDocument()
  })
})
