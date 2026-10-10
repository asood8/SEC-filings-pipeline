import { Link } from 'react-router'
import { usePageTitle } from '../lib/usePageTitle'

export function NotFoundPage({ what = 'page' }: { what?: string }) {
  usePageTitle(`${what.charAt(0).toUpperCase()}${what.slice(1)} not found`)
  return (
    <>
      <h1>Not found</h1>
      <p>
        That {what} doesn't exist here.{' '}
        <Link to="/">See the latest filings</Link>.
      </p>
    </>
  )
}
