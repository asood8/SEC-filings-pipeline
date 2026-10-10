import { isRouteErrorResponse, Link, useRouteError } from 'react-router'
import { usePageTitle } from '../lib/usePageTitle'
import { NotFoundPage } from './NotFoundPage'

/** Shown instead of a blank page when a page crashes. It replaces the whole layout, so it
 * brings its own <main>. */
export function ErrorPage() {
  const error = useRouteError()
  const notFound = isRouteErrorResponse(error) && error.status === 404
  usePageTitle(notFound ? 'Page not found' : 'Something went wrong')
  return (
    <main style={{ maxWidth: '40rem', margin: '3rem auto', padding: '0 1rem' }}>
      {notFound ? (
        <NotFoundPage />
      ) : (
        <>
          <h1>Something went wrong</h1>
          <p>
            This page hit an unexpected error. Try <a href="">reloading it</a>,
            or go back to the <Link to="/">latest filings</Link>.
          </p>
        </>
      )}
    </main>
  )
}
