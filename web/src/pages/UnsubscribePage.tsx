import { useQuery } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router'
import { ApiError } from '../api/client'
import { unsubscribeQuery, useUnsubscribe } from '../api/account'
import { ErrorMessage, Loading } from '../components/QueryStatus'
import { usePageTitle } from '../lib/usePageTitle'

/** The confirmation page an alert email links to: one button stops that email alert, with no
 * sign-in (the link is signed for it). Opening the page changes nothing, so a mail scanner
 * that follows the link can't unsubscribe anyone. */
export function UnsubscribePage() {
  usePageTitle('Stop email alerts')
  const [params] = useSearchParams()
  const token = params.get('token') ?? ''
  const alert = useQuery({ ...unsubscribeQuery(token), enabled: token !== '' })
  const stop = useUnsubscribe(token)

  if (stop.isSuccess) {
    return (
      <>
        <h1>Unsubscribed</h1>
        <p role="status">
          You won&apos;t get these emails any more.{' '}
          <Link to="/alerts">Manage your other alerts</Link>.
        </p>
      </>
    )
  }
  if (
    token === '' ||
    (alert.error instanceof ApiError && alert.error.status === 400)
  ) {
    return (
      <Gone message="This unsubscribe link isn't valid. Check that it was copied whole." />
    )
  }
  if (alert.error instanceof ApiError && alert.error.status === 404) {
    return (
      <Gone message="This alert is already gone: you won't get these emails." />
    )
  }
  if (alert.isPending) return <Loading />
  if (alert.isError) return <ErrorMessage error={alert.error} />

  const { company, query, target } = alert.data
  const what = [
    company ? `${company.name} (${company.ticker})` : 'every watchlist company',
    query ? `mentioning “${query}”` : null,
  ]
    .filter(Boolean)
    .join(', ')
  return (
    <>
      <h1>Stop email alerts</h1>
      <p>
        Stop the alerts about {what} sent to {target}? Your other alerts stay as
        they are.
      </p>
      <button
        type="button"
        onClick={() => stop.mutate()}
        disabled={stop.isPending}
      >
        {stop.isPending ? 'Stopping…' : 'Stop these alerts'}
      </button>
      {stop.isError && <ErrorMessage error={stop.error} />}
    </>
  )
}

function Gone({ message }: { message: string }) {
  return (
    <>
      <h1>Stop email alerts</h1>
      <p className="notice">{message}</p>
      <p>
        <Link to="/alerts">See your alerts</Link>
      </p>
    </>
  )
}
