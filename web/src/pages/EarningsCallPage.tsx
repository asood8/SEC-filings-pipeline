import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router'
import { isNotFound, type CallExchange } from '../api/client'
import { callSummaryQuery } from '../api/queries'
import { ExternalLink } from '../components/ExternalLink'
import { ErrorMessage, Loading } from '../components/QueryStatus'
import { formatDate } from '../lib/format'
import { usePageTitle } from '../lib/usePageTitle'
import { NotFoundPage } from './NotFoundPage'
import styles from './EarningsCallPage.module.css'

/** The host of a transcript's address, to say whose site it is on ("investor.example.com"). */
function siteOf(url: string): string {
  try {
    return new URL(url).host
  } catch {
    return url
  }
}

/** An automated summary of one earnings call's Q&A: the takeaways, then each analyst's question
 * and management's answer, with a link to the transcript on the company's own site. The
 * transcript itself is never shown (ADR-0022). */
export function EarningsCallPage() {
  const { id = '' } = useParams()
  const number = Number(id)
  const valid = Number.isInteger(number) && number > 0
  const call = useQuery({ ...callSummaryQuery(number), enabled: valid })
  usePageTitle(
    call.data
      ? `${call.data.ticker} earnings call, ${formatDate(call.data.call_date)}`
      : 'Earnings call',
  )

  if (!valid || isNotFound(call.error)) return <NotFoundPage what="call" />
  if (call.isPending) return <Loading />
  if (call.isError) return <ErrorMessage error={call.error} />

  const summary = call.data
  const failed = summary.exchanges.filter((x) => x.failed).length
  return (
    <>
      <h1>{summary.headline}</h1>
      <p className="muted">
        <Link to={`/companies/${summary.ticker}`}>{summary.company_name}</Link>{' '}
        · earnings call of{' '}
        <time dateTime={summary.call_date}>
          {formatDate(summary.call_date)}
        </time>{' '}
        ·{' '}
        <ExternalLink href={summary.source_url}>
          Full transcript on {siteOf(summary.source_url)}
        </ExternalLink>
      </p>
      <section>
        <h2>Takeaways from the Q&amp;A</h2>
        <p>{summary.summary}</p>
        {summary.key_points.length > 0 && (
          <ul>
            {summary.key_points.map((point) => (
              <li key={point}>{point}</li>
            ))}
          </ul>
        )}
        {summary.ungrounded_numbers.length > 0 && (
          <p className="notice">
            Check these numbers against the transcript:{' '}
            {summary.ungrounded_numbers.join(', ')}. They appear in the summary
            but not in the transcript.
          </p>
        )}
        {summary.ungrounded_names.length > 0 && (
          <p className="notice">
            Check these names against the transcript:{' '}
            {summary.ungrounded_names.join(', ')}. The summary uses them, but
            the transcript doesn't.
          </p>
        )}
      </section>
      <section>
        <h2>Questions and answers</h2>
        <ol className={styles.exchanges}>
          {summary.exchanges.map((exchange, index) => (
            <Exchange
              key={`${index}-${exchange.analyst}`}
              exchange={exchange}
            />
          ))}
        </ol>
        {failed > 0 && (
          <p className="muted">
            {failed === 1 ? 'One question' : `${failed} questions`} couldn't be
            summarized; the transcript has {failed === 1 ? 'it' : 'them'}.
          </p>
        )}
      </section>
      <p className="muted">
        Summarized by an AI model ({summary.model}) from the company's own
        transcript, so it may contain errors. Read the transcript before relying
        on it.
      </p>
    </>
  )
}

function Exchange({ exchange }: { exchange: CallExchange }) {
  const unverified = exchange.ungrounded_numbers ?? []
  return (
    <li className={styles.exchange}>
      <p className={styles.analyst}>
        <strong>{exchange.analyst}</strong>
        <span className="muted"> · {exchange.firm}</span>
      </p>
      {exchange.failed ? (
        <p className="muted">This question couldn't be summarized.</p>
      ) : (
        <>
          <p>
            <span className={styles.label}>Asked:</span> {exchange.question}
          </p>
          <p>
            <span className={styles.label}>Answer:</span> {exchange.answer}
          </p>
          {unverified.length > 0 && (
            <p className="notice">
              Check against the transcript: {unverified.join(', ')}.
            </p>
          )}
        </>
      )}
    </li>
  )
}
