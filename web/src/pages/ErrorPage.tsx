import { isRouteErrorResponse, Link, useRouteError } from 'react-router'
import { NotFoundPage } from './NotFoundPage'

/** Shown instead of a blank page when a page crashes. It replaces the whole layout, so it
 * brings its own <main>. */
export function ErrorPage() {
  const error = useRouteError()
  return (
    <main style={{ maxWidth: '40rem', margin: '3rem auto', padding: '0 1rem' }}>
      {isRouteErrorResponse(error) && error.status === 404 ? (
        <NotFoundPage />
      ) : (
        <>
          <title>Something went wrong · SEC Filings</title>
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
