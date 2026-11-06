import { useQuery } from '@tanstack/react-query'
import { type FormEvent, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import {
  meQuery,
  myCompanyRequestsQuery,
  useSuggestCompany,
} from '../api/account'
import type { CompanyRequest, User } from '../api/client'
import { ErrorMessage, Loading } from '../components/QueryStatus'
import { SignInPrompt } from '../components/SignInPrompt'
import { formatDayOf } from '../lib/format'
import { useFocusWhenSignedInOrOut } from '../lib/useFocusWhenSignedInOrOut'
import { usePageTitle } from '../lib/usePageTitle'
import styles from './Suggestions.module.css'

/** Signed-in users ask for a company the site doesn't follow yet; the admin reads the requests
 * (CompanySuggestionsPage) and adds the most-asked-for ones by hand. */
export function SuggestCompanyPage() {
  const me = useQuery(meQuery())
  const heading = useRef<HTMLHeadingElement>(null)
  useFocusWhenSignedInOrOut(me.data, heading)
  usePageTitle('Suggest a company')

  return (
    <>
      <h1 ref={heading} tabIndex={-1}>
        Suggest a company
      </h1>
      <p className="muted">
        Want filings from a company that isn&apos;t on the{' '}
        <Link to="/companies">watchlist</Link>? Ask for it. Every suggestion is
        read, and the companies people ask for most are added first.
      </p>
      {me.isPending ? (
        <Loading />
      ) : me.isError ? (
        <ErrorMessage error={me.error} />
      ) : me.data ? (
        <SignedIn user={me.data} />
      ) : (
        <SignInPrompt>
          Sign in with your Google account to suggest a company, so each person
          counts once.
        </SignInPrompt>
      )}
    </>
  )
}

function SignedIn({ user }: { user: User }) {
  const [params] = useSearchParams()
  const mine = useQuery(myCompanyRequestsQuery())
  const suggest = useSuggestCompany()
  const [thanks, setThanks] = useState('')
  const [ticker, setTicker] = useState(params.get('ticker') ?? '')

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = event.currentTarget
    const data = new FormData(form)
    const note = String(data.get('note') ?? '').trim()
    setThanks('')
    suggest.mutate(
      { ticker: ticker.trim(), note: note || null },
      {
        onSuccess: (request) => {
          setThanks(
            `Thanks: ${request.company.name} (${request.company.ticker}) is suggested.`,
          )
          form.reset()
          setTicker('')
        },
      },
    )
  }

  return (
    <>
      <form className={styles.form} onSubmit={submit}>
        <label>
          Ticker
          <input
            name="ticker"
            required
            maxLength={20}
            autoComplete="off"
            autoCapitalize="characters"
            spellCheck={false}
            value={ticker}
            onChange={(event) => setTicker(event.target.value)}
            aria-describedby="ticker-hint"
          />
        </label>
        <p id="ticker-hint" className={`muted ${styles.hint}`}>
          Its stock ticker, like NOW for ServiceNow or BRK.B for Berkshire
          Hathaway. Any company that files with the SEC.
        </p>
        <label>
          Why follow it? (optional)
          <textarea
            name="note"
            rows={3}
            maxLength={500}
            aria-describedby="note-hint"
          />
        </label>
        <p id="note-hint" className={`muted ${styles.hint}`}>
          Only the site&apos;s admin reads suggestions, with your email partly
          hidden.
        </p>
        <button type="submit" disabled={suggest.isPending}>
          {suggest.isPending ? 'Sending…' : 'Suggest'}
        </button>
        <p role="status" className={styles.hint}>
          {thanks}
        </p>
        {suggest.isError && <ErrorMessage error={suggest.error} />}
      </form>

      <h2>Your suggestions</h2>
      {mine.isPending ? (
        <Loading />
      ) : mine.isError ? (
        <ErrorMessage error={mine.error} />
      ) : mine.data.length === 0 ? (
        <p className="muted">None yet.</p>
      ) : (
        <ul className={styles.list}>
          {mine.data.map((request) => (
            <li key={request.id} className={styles.item}>
              <span className={styles.ticker}>{request.company.ticker}</span>{' '}
              {request.company.name}
              <p className={styles.status}>
                <RequestStatusText request={request} />
              </p>
            </li>
          ))}
        </ul>
      )}

      {user.is_admin && (
        <p>
          <Link to="/admin/suggestions">
            Review everyone&apos;s suggestions
          </Link>
        </p>
      )}
    </>
  )
}

function RequestStatusText({ request }: { request: CompanyRequest }) {
  const asked = `Suggested ${formatDayOf(request.created_at)}`
  const decided = request.decided_at ? formatDayOf(request.decided_at) : ''
  switch (request.status) {
    case 'open':
      return <>{asked}: waiting for a decision.</>
    case 'added':
      return (
        <>
          Added {decided}:{' '}
          <Link to={`/companies/${request.company.ticker}`}>
            see its filings
          </Link>
          .
        </>
      )
    case 'declined':
      return (
        <>
          {asked}: not added ({decided}).
        </>
      )
  }
}
