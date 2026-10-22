import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router'
import { API_BASE_URL, type UpcomingCall } from '../api/client'
import { callsQuery } from '../api/queries'
import { ExternalLink } from '../components/ExternalLink'
import { ErrorMessage, Loading } from '../components/QueryStatus'
import { formatEasternClock, formatWeekday } from '../lib/format'
import { usePageTitle } from '../lib/usePageTitle'
import styles from './CallsPage.module.css'

const DAYS = 60

/** The calls in day order, grouped by day. */
function byDay(calls: UpcomingCall[]): [string, UpcomingCall[]][] {
  const days = new Map<string, UpcomingCall[]>()
  for (const call of calls)
    days.set(call.day, [...(days.get(call.day) ?? []), call])
  return [...days]
}

/** Earnings calls and other investor events the watchlist's 8-Ks announce, with a calendar feed
 * to subscribe to. */
export function CallsPage() {
  usePageTitle('Upcoming calls')
  const query = useQuery(callsQuery(DAYS))
  const feed = `${API_BASE_URL}/calls.ics`

  return (
    <>
      <h1>Upcoming calls</h1>
      <p>
        Earnings calls, Q&amp;A sessions, and investor days that watchlist
        companies announced in their 8-Ks, for the next {DAYS} days. Times are
        US Eastern. The calls themselves are on the companies' websites.
      </p>
      <p>
        <ExternalLink href={feed}>Calendar feed</ExternalLink>{' '}
        <span className="muted">
          Subscribe once and every announced call appears in your calendar: in
          Google Calendar, Other calendars, From URL, and paste {feed}
        </span>
      </p>
      {query.isPending ? (
        <Loading />
      ) : query.isError ? (
        <ErrorMessage error={query.error} />
      ) : query.data.items.length === 0 ? (
        <p className="notice">No calls announced for the next {DAYS} days.</p>
      ) : (
        byDay(query.data.items).map(([day, calls]) => (
          <section key={day} className={styles.day}>
            <h2>
              <time dateTime={day}>{formatWeekday(day)}</time>
            </h2>
            <ul className={styles.calls}>
              {calls.map((call) => (
                <Call key={`${call.ticker}-${call.day}`} call={call} />
              ))}
            </ul>
          </section>
        ))
      )}
    </>
  )
}

function Call({ call }: { call: UpcomingCall }) {
  return (
    <li className={styles.call}>
      <span className={styles.time}>
        {call.starts_at ? (
          <time dateTime={call.starts_at}>
            {formatEasternClock(call.starts_at)}
          </time>
        ) : (
          <span className="muted">Time not given</span>
        )}
      </span>
      <Link to={`/companies/${call.ticker}`}>
        <strong>{call.ticker}</strong>
      </Link>
      <span>
        {call.company_name}: {call.kind}
      </span>
      {call.webcast_url && /^https?:\/\//.test(call.webcast_url) && (
        <ExternalLink href={call.webcast_url}>Webcast</ExternalLink>
      )}
      <Link to={`/filings/${call.accession_no}`}>Filing</Link>
    </li>
  )
}
