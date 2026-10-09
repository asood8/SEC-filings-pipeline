import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { mockApi, renderRoute } from '../test/render'

const EMPTY = { items: [], next_cursor: null }

describe('Layout', () => {
  it('has the page landmarks, with a labelled search box', async () => {
    mockApi({ '/filings': () => EMPTY })
    renderRoute('/')
    await screen.findByText('No filings yet.')

    expect(screen.getByRole('banner')).toBeInTheDocument()
    expect(screen.getByRole('navigation', { name: 'Main' })).toBeInTheDocument()
    expect(screen.getByRole('main')).toBeInTheDocument()
    expect(screen.getByRole('contentinfo')).toBeInTheDocument()
    const search = screen.getByRole('search')
    expect(within(search).getByLabelText('Search filings')).toHaveAttribute(
      'type',
      'search',
    )
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1)
  })

  it('starts with a skip link to the main content', async () => {
    mockApi({ '/filings': () => EMPTY })
    renderRoute('/')
    await screen.findByText('No filings yet.')

    await userEvent.tab()
    const skip = screen.getByRole('link', { name: 'Skip to main content' })
    expect(skip).toHaveFocus()
    await userEvent.keyboard('{Enter}')
    expect(screen.getByRole('main')).toHaveFocus()
    // Focus moved without a navigation of its own.
    expect(window.location.hash).toBe('')
  })

  it("moves focus to the new page's heading after following a link", async () => {
    mockApi({
      '/filings': () => EMPTY,
      '/companies': () => EMPTY,
    })
    renderRoute('/')
    await screen.findByText('No filings yet.')
    expect(document.body).toHaveFocus() // the first load leaves focus alone

    await userEvent.click(screen.getByRole('link', { name: 'Companies' }))
    expect(
      await screen.findByRole('heading', { level: 1, name: 'Companies' }),
    ).toHaveFocus()
  })
})
