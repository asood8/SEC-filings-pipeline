import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { filing } from '../test/data'
import { mockApi, renderRoute } from '../test/render'

describe('SearchPage', () => {
  it('searches from the URL and notes degraded results', async () => {
    const requests = mockApi({
      '/search': (url) => ({
        query: url.searchParams.get('q'),
        mode: 'keyword',
        degraded: true,
        items: [{ ...filing(), score: 0.5 }],
      }),
    })
    renderRoute('/search?q=new%20cfo&mode=semantic')

    expect(
      await screen.findByRole('link', { name: /Kevan Parekh/ }),
    ).toBeInTheDocument()
    expect(screen.getByText(/match exact words only/)).toBeInTheDocument()
    // Screen readers hear how many filings matched.
    expect(screen.getByRole('status')).toHaveTextContent(
      '1 match for “new cfo”.',
    )
    expect(requests[0].searchParams.get('q')).toBe('new cfo')
    expect(requests[0].searchParams.get('mode')).toBe('semantic')
  })

  it('submits the form into the URL', async () => {
    const requests = mockApi({
      '/search': () => ({
        query: 'buyback',
        mode: 'hybrid',
        degraded: false,
        items: [],
      }),
    })
    const router = renderRoute('/search')
    expect(requests).toHaveLength(0) // nothing to search for yet

    const input = screen.getByLabelText('Search for')
    expect(input).toHaveAccessibleDescription(/quoted phrases/)
    await userEvent.type(input, 'buyback')
    await userEvent.selectOptions(screen.getByLabelText('Form type'), '8-K')
    await userEvent.click(screen.getByRole('button', { name: 'Search' }))

    expect(
      await screen.findByText('No filings match “buyback”.'),
    ).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent(
      'No filings match “buyback”.',
    )
    // The form is updated in place, so focus stays on the button just used.
    expect(screen.getByRole('button', { name: 'Search' })).toHaveFocus()
    expect(screen.getByLabelText('Search for')).toHaveValue('buyback')
    expect(screen.getByLabelText('Match')).toHaveValue('hybrid')
    expect(router.state.location.search).toBe('?q=buyback&mode=hybrid&form=8-K')
    expect(requests[0].searchParams.get('form_type')).toBe('8-K')
  })

  it("fills the form from the header's search box", async () => {
    mockApi({
      '/search': (url) => ({
        query: url.searchParams.get('q'),
        mode: 'hybrid',
        degraded: false,
        items: [],
      }),
    })
    renderRoute('/search?q=buyback')
    expect(await screen.findByLabelText('Search for')).toHaveValue('buyback')

    const header = screen.getByRole('search')
    await userEvent.type(
      within(header).getByLabelText('Search filings'),
      'dividend{Enter}',
    )
    expect(
      await screen.findByText('No filings match “dividend”.'),
    ).toBeVisible()
    expect(screen.getByLabelText('Search for')).toHaveValue('dividend')
  })
})
