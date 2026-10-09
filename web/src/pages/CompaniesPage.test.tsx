import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { mockApi, renderRoute } from '../test/render'

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
      name: 'Microsoft Corp',
      exchange: 'Nasdaq',
      on_watchlist: true,
    },
  ],
  next_cursor: null,
}

describe('CompaniesPage', () => {
  it('filters the watchlist, telling screen readers how many match', async () => {
    mockApi({ '/companies': () => WATCHLIST })
    renderRoute('/companies')

    expect(await screen.findByRole('link', { name: /AAPL/ })).toHaveAttribute(
      'href',
      '/companies/AAPL',
    )
    expect(screen.getByRole('status')).toHaveTextContent('')

    await userEvent.type(screen.getByLabelText('Filter companies'), 'micro')
    expect(screen.queryByRole('link', { name: /AAPL/ })).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: /MSFT/ })).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent(
      '1 of 2 companies match.',
    )

    await userEvent.type(screen.getByLabelText('Filter companies'), 'z')
    expect(screen.getByText('No companies match “microz”.')).toBeVisible()
    expect(screen.getByRole('status')).toHaveTextContent(
      '0 of 2 companies match.',
    )
  })
})
