import { screen } from '@testing-library/react'
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

    await userEvent.type(screen.getByLabelText('Search for'), 'buyback')
    await userEvent.selectOptions(screen.getByLabelText('Form type'), '8-K')
    await userEvent.click(screen.getByRole('button', { name: 'Search' }))

    expect(
      await screen.findByText('No filings match “buyback”.'),
    ).toBeInTheDocument()
    expect(router.state.location.search).toBe('?q=buyback&mode=hybrid&form=8-K')
    expect(requests[0].searchParams.get('form_type')).toBe('8-K')
  })
})
