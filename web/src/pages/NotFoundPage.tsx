import { Link } from 'react-router'

export function NotFoundPage({ what = 'page' }: { what?: string }) {
  return (
    <>
      <title>Not found · SEC Filings</title>
      <h1>Not found</h1>
      <p>
        That {what} doesn't exist here.{' '}
        <Link to="/">See the latest filings</Link>.
      </p>
    </>
  )
}
