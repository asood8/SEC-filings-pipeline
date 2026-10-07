import { render, screen } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import { ErrorPage } from './ErrorPage'

function Broken(): never {
  throw new Error('boom')
}

describe('ErrorPage', () => {
  it('replaces a crashed page with a way back', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {}) // React logs the thrown error
    const router = createMemoryRouter([
      { path: '/', element: <Broken />, errorElement: <ErrorPage /> },
    ])
    render(<RouterProvider router={router} />)
    expect(
      await screen.findByRole('heading', { name: 'Something went wrong' }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('link', { name: 'latest filings' }),
    ).toHaveAttribute('href', '/')
  })
})
