import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { mockApi, renderRoute } from '../test/render'

describe('AboutPage', () => {
  it('says where the data comes from, how fresh it is, and its limits', async () => {
    mockApi({})
    renderRoute('/about')

    expect(
      await screen.findByRole('heading', {
        level: 1,
        name: 'About SEC Filings',
      }),
    ).toBeInTheDocument()
    expect(document.title).toBe('About · SEC Filings')
    for (const name of [
      'What it covers',
      'Where the data comes from',
      'How fresh it is',
      'How summaries are written',
      'Limits',
    ])
      expect(
        screen.getByRole('heading', { level: 2, name }),
      ).toBeInTheDocument()

    const edgar = screen.getByRole('link', { name: /SEC EDGAR/ })
    expect(edgar).toHaveAttribute('href', 'https://www.sec.gov/edgar/search/')
    expect(edgar).toHaveAttribute('target', '_blank')
    expect(screen.getByText(/every 10 minutes/)).toBeInTheDocument()
    expect(screen.getByText(/isn't affiliated with/)).toBeInTheDocument()
    expect(
      screen.getByText('Nothing on this site is investment advice.'),
    ).toBeInTheDocument()
  })

  it('is linked from the footer of every page', async () => {
    mockApi({ '/filings': () => ({ items: [], next_cursor: null }) })
    renderRoute('/')
    await screen.findByText('No filings yet.')

    const footer = screen.getByRole('contentinfo')
    await userEvent.click(within(footer).getByRole('link', { name: 'About' }))
    expect(
      await screen.findByRole('heading', {
        level: 1,
        name: 'About SEC Filings',
      }),
    ).toHaveFocus()
  })
})
