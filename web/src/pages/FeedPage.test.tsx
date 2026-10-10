import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { filing } from '../test/data'
import { mockApi, renderRoute } from '../test/render'

describe('FeedPage', () => {
  it('lists filings and loads the next page with the cursor', async () => {
    const requests = mockApi({
      '/filings': (url) =>
        url.searchParams.get('cursor') === 'page-2'
          ? {
              items: [filing({ accession_no: 'b', headline: 'Second page' })],
              next_cursor: null,
            }
          : {
              items: [
                filing({ accession_no: 'a' }),
                filing({ accession_no: 'p', form_type: '4', headline: null }),
              ],
              next_cursor: 'page-2',
            },
    })
    renderRoute('/')

    expect(
      await screen.findByRole('link', { name: /Kevan Parekh/ }),
    ).toHaveAttribute('href', '/filings/a')
    expect(document.title).toBe('Latest filings · SEC Filings')
    expect(
      screen.getByText('Form 4 filing, summary pending'),
    ).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Load more' }))
    // Focus moves to the first new filing (the button is gone after the last page).
    await waitFor(() =>
      expect(screen.getByRole('link', { name: 'Second page' })).toHaveFocus(),
    )
    expect(
      screen.queryByRole('button', { name: 'Load more' }),
    ).not.toBeInTheDocument()
    expect(requests.at(-1)?.searchParams.get('cursor')).toBe('page-2')
  })

  it('filters by form type', async () => {
    const requests = mockApi({
      '/filings': () => ({ items: [], next_cursor: null }),
    })
    const router = renderRoute('/')
    await screen.findByText('No filings yet.')

    await userEvent.click(screen.getByLabelText('Insider trades (Form 4)'))
    await screen.findByText('No filings yet.')
    expect(router.state.location.search).toBe('?form=4')
    expect(requests.at(-1)?.searchParams.get('form_type')).toBe('4')
  })

  it('keeps the filings loaded so far when the next page fails', async () => {
    mockApi({
      '/filings': (url) =>
        url.searchParams.get('cursor')
          ? new Response(null, { status: 503 })
          : { items: [filing()], next_cursor: 'page-2' },
    })
    renderRoute('/')

    await userEvent.click(
      await screen.findByRole('button', { name: 'Load more' }),
    )
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The server had a problem',
    )
    expect(screen.getByRole('link', { name: /Kevan Parekh/ })).toBeVisible()
    expect(screen.getByRole('button', { name: 'Load more' })).toBeVisible()
  })

  it('explains API errors', async () => {
    mockApi({ '/filings': () => new Response(null, { status: 503 }) })
    renderRoute('/')
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The server had a problem',
    )
  })
})
