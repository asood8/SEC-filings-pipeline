import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { mockApi, renderRoute } from '../test/render'

describe('the privacy and terms pages', () => {
  it('says what is kept and how to delete it', async () => {
    mockApi({})
    renderRoute('/privacy')
    expect(
      await screen.findByRole('heading', { level: 1, name: 'Privacy' }),
    ).toBeInTheDocument()
    expect(document.title).toBe('Privacy · Filingwire')
    for (const name of [
      'If you only read the site',
      'If you sign in',
      'Cookies',
      'Who else handles it',
      'Seeing, changing, and deleting it',
    ])
      expect(
        screen.getByRole('heading', { level: 2, name }),
      ).toBeInTheDocument()
    expect(screen.getByText(/removes your account, every alert/)).toBeVisible()
    expect(
      screen.getAllByRole('link', { name: 'hello@filingwire.app' })[0],
    ).toHaveAttribute('href', 'mailto:hello@filingwire.app')
  })

  it('states the terms, and both are linked from every page', async () => {
    mockApi({})
    renderRoute('/terms')
    expect(
      await screen.findByRole('heading', { level: 1, name: 'Terms of use' }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('heading', { level: 2, name: 'Not investment advice' }),
    ).toBeInTheDocument()
    const footer = within(screen.getByRole('contentinfo'))
    expect(footer.getByRole('link', { name: 'Privacy' })).toHaveAttribute(
      'href',
      '/privacy',
    )
    expect(footer.getByRole('link', { name: 'Terms' })).toHaveAttribute(
      'href',
      '/terms',
    )
  })
})
