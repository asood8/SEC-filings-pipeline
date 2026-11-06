import { useQuery } from '@tanstack/react-query'
import { useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import {
  companySuggestionsQuery,
  meQuery,
  useDecideSuggestion,
} from '../api/account'
import type { CompanySuggestion, RequestStatus } from '../api/client'
import { ErrorMessage, Loading } from '../components/QueryStatus'
import { SignInPrompt } from '../components/SignInPrompt'
import { formatDayOf } from '../lib/format'
import { useFocusWhenSignedInOrOut } from '../lib/useFocusWhenSignedInOrOut'
import { usePageTitle } from '../lib/usePageTitle'
import styles from './Suggestions.module.css'

const SHOWN: Record<RequestStatus, string> = {
  open: 'Waiting for a decision',
  added: 'Added',
  declined: 'Declined',
}

const DONE: Record<RequestStatus, string> = {
  open: 'reopened',
  added: 'marked added',
  declined: 'declined',
}

function shownStatus(value: string | null): RequestStatus {
  return value === 'added' || value === 'declined' ? value : 'open'
}

/** The admin's review of company suggestions (admin rights come from `filings-admin grant`). */
export function CompanySuggestionsPage() {
  const me = useQuery(meQuery())
  const heading = useRef<HTMLHeadingElement>(null)
  useFocusWhenSignedInOrOut(me.data, heading)
  usePageTitle('Company suggestions')

  return (
    <>
      <h1 ref={heading} tabIndex={-1}>
        Company suggestions
      </h1>
      {me.isPending ? (
        <Loading />
      ) : me.isError ? (
        <ErrorMessage error={me.error} />
      ) : !me.data ? (
        <SignInPrompt>Sign in to review suggestions.</SignInPrompt>
      ) : !me.data.is_admin ? (
        <p className="notice">
          Only the site&apos;s admin can review suggestions.{' '}
          <Link to="/suggest">Suggest a company</Link> instead.
        </p>
      ) : (
        <Review />
      )}
    </>
  )
}

function Review() {
  const [params, setParams] = useSearchParams()
  const status = shownStatus(params.get('status'))
  const suggestions = useQuery(companySuggestionsQuery(status))
  const decide = useDecideSuggestion()
  const [done, setDone] = useState('')
  const listHeading = useRef<HTMLHeadingElement>(null)

  function choose(ticker: string, next: RequestStatus) {
    setDone('')
    decide.mutate(
      { ticker, status: next },
      {
        onSuccess: () => {
          setDone(`${ticker} ${DONE[next]}.`)
          // Its buttons leave the list with it, so focus goes to the list's heading.
          listHeading.current?.focus()
        },
      },
    )
  }

  return (
    <>
      <p className="muted">
        Most asked-for first. To add one, put it in config/watchlist.toml and
        load the watchlist (runbook: Adding a company), then mark it added:
        everyone who suggested it sees that.
      </p>
      <p>
        <label>
          Show{' '}
          <select
            value={status}
            onChange={(event) => {
              setDone('')
              setParams(
                event.target.value === 'open'
                  ? {}
                  : { status: event.target.value },
              )
            }}
          >
            {Object.entries(SHOWN).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
      </p>
      <p role="status">{done}</p>
      <h2 id="suggestions-heading" ref={listHeading} tabIndex={-1}>
        {SHOWN[status]}
        {suggestions.data && ` (${suggestions.data.length})`}
      </h2>
      {suggestions.isPending ? (
        <Loading />
      ) : suggestions.isError ? (
        <ErrorMessage error={suggestions.error} />
      ) : suggestions.data.length === 0 ? (
        <p className="muted">None.</p>
      ) : (
        <ul className={styles.list} aria-labelledby="suggestions-heading">
          {suggestions.data.map((suggestion) => {
            const { ticker } = suggestion.company
            const deciding =
              decide.isPending && decide.variables.ticker === ticker
            const failed = decide.isError && decide.variables.ticker === ticker
            return (
              <li key={suggestion.company.cik} className={styles.item}>
                <div className={styles.row}>
                  <Summary suggestion={suggestion} />
                  <div className={styles.actions}>
                    {status === 'open' ? (
                      <>
                        <button
                          type="button"
                          disabled={deciding}
                          onClick={() => choose(ticker, 'added')}
                        >
                          Mark {ticker} added
                        </button>
                        <button
                          type="button"
                          disabled={deciding}
                          onClick={() => choose(ticker, 'declined')}
                        >
                          Decline {ticker}
                        </button>
                      </>
                    ) : (
                      <button
                        type="button"
                        disabled={deciding}
                        onClick={() => choose(ticker, 'open')}
                      >
                        Reopen {ticker}
                      </button>
                    )}
                  </div>
                </div>
                {failed && <ErrorMessage error={decide.error} />}
                {suggestion.notes.length > 0 && (
                  <ul
                    className={styles.notes}
                    aria-label={`Notes on ${ticker}`}
                  >
                    {suggestion.notes.map((note) => (
                      <li key={`${note.by} ${note.at}`}>
                        <p>“{note.note}”</p>
                        <p className={styles.status}>
                          {note.by}, {formatDayOf(note.at)}
                        </p>
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </>
  )
}

function Summary({ suggestion }: { suggestion: CompanySuggestion }) {
  const { company, requests, last_requested_at, on_watchlist } = suggestion
  const people = requests === 1 ? '1 person' : `${requests} people`
  return (
    <div>
      <span className={styles.ticker}>{company.ticker}</span> {company.name}
      <p className={styles.status}>
        {people} asked, last on {formatDayOf(last_requested_at)}.{' '}
        {on_watchlist ? (
          <Link to={`/companies/${company.ticker}`}>On the watchlist</Link>
        ) : (
          'Not on the watchlist.'
        )}
      </p>
    </div>
  )
}
