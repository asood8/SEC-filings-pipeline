import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router'
import { isNotFound, type FilingDetail } from '../api/client'
import { filingQuery } from '../api/queries'
import { ErrorMessage, Loading } from '../components/QueryStatus'
import { formatDate, formatDateTime, formLabel } from '../lib/format'
import { ITEMS_8K, TRANSACTION_CODES } from '../lib/glossary'
import { NotFoundPage } from './NotFoundPage'
import styles from './FilingPage.module.css'

export function FilingPage() {
  const { accessionNo = '' } = useParams()
  const query = useQuery(filingQuery(accessionNo))

  if (query.isPending) return <Loading />
  if (isNotFound(query.error)) return <NotFoundPage what="filing" />
  if (query.isError) return <ErrorMessage error={query.error} />

  const filing = query.data
  const title = filing.headline ?? `${formLabel(filing.form_type)} filing`
  return (
    <article>
      <title>{`${title} · SEC Filings`}</title>
      <p className={styles.meta}>
        <Link to={`/companies/${filing.ticker}`} className={styles.ticker}>
          {filing.ticker}
        </Link>
        <span>{filing.company_name}</span>
        <span className="badge">{formLabel(filing.form_type)}</span>
        <span>
          Filed{' '}
          <time dateTime={filing.filed_at}>{formatDate(filing.filed_at)}</time>
        </span>
      </p>
      <h1>{title}</h1>
      <p>
        <a href={filing.sec_url} target="_blank" rel="noreferrer">
          Read the original on sec.gov ↗
        </a>
      </p>

      <Summary filing={filing} />
      {filing.form4 && <Form4Section filing={filing} form4={filing.form4} />}
      {filing.items && filing.items.length > 0 && (
        <section>
          <h2>Items reported</h2>
          <ul>
            {filing.items.map((item) => (
              <li key={item}>
                <strong>{item}</strong> {ITEMS_8K[item] ?? ''}
              </li>
            ))}
          </ul>
        </section>
      )}
      {filing.exhibits.length > 0 && (
        <section>
          <h2>Exhibits</h2>
          <ul>
            {filing.exhibits.map((exhibit) => (
              <li key={exhibit.filename}>
                <a href={exhibit.sec_url} target="_blank" rel="noreferrer">
                  {exhibit.type}
                </a>{' '}
                <span className="muted">{exhibit.filename}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      <section>
        <h2>Filing details</h2>
        <dl className={styles.details}>
          <dt>Accession number</dt>
          <dd>{filing.accession_no}</dd>
          <dt>Filer CIK</dt>
          <dd>{filing.cik}</dd>
          {filing.accepted_at && (
            <>
              <dt>Accepted by the SEC</dt>
              <dd>{formatDateTime(filing.accepted_at)}</dd>
            </>
          )}
        </dl>
      </section>
    </article>
  )
}

function Summary({ filing }: { filing: FilingDetail }) {
  const summary = filing.summary
  if (!summary) {
    return (
      <p className="notice">
        This filing hasn't been summarized yet. The original is on sec.gov.
      </p>
    )
  }
  const unverified = summary.ungrounded_numbers ?? []
  return (
    <section>
      <h2>Summary</h2>
      <p>{summary.summary}</p>
      {summary.key_points.length > 0 && (
        <ul>
          {summary.key_points.map((point) => (
            <li key={point}>{point}</li>
          ))}
        </ul>
      )}
      {unverified.length > 0 && (
        <p className="notice">
          Check these numbers against the original: {unverified.join(', ')}.
          They appear in the summary but weren't found in the filing's text.
        </p>
      )}
      <p className="muted">
        {summary.provider === 'template'
          ? "Written from the filing's structured data."
          : `Summarized by an AI model (${summary.model}), so it may contain errors.`}
      </p>
    </section>
  )
}

type Form4 = NonNullable<FilingDetail['form4']>

function Form4Section({
  filing,
  form4,
}: {
  filing: FilingDetail
  form4: Form4
}) {
  const issuer = form4.issuer_ticker ? (
    <Link to={`/companies/${form4.issuer_ticker}`}>{form4.issuer_name}</Link>
  ) : (
    form4.issuer_name
  )
  return (
    <section>
      <h2>Insider trade</h2>
      <dl className={styles.details}>
        <dt>Stock traded</dt>
        <dd>
          {issuer}
          {form4.issuer_cik !== filing.cik && (
            <span className="muted"> (filed by {filing.company_name})</span>
          )}
        </dd>
        <dt>Reported by</dt>
        <dd>{form4.reporting_owners.join('; ')}</dd>
        <dt>Transactions</dt>
        <dd>
          <ul className={styles.codes}>
            {form4.transaction_codes.map((code) => (
              <li key={code}>
                <strong>{code}</strong> {TRANSACTION_CODES[code] ?? ''}
              </li>
            ))}
          </ul>
        </dd>
        <dt>Date of the trades</dt>
        <dd>{formatDate(form4.period_of_report)}</dd>
        {form4.is_10b5_1 !== null && (
          <>
            <dt>Trading plan</dt>
            <dd>
              {form4.is_10b5_1
                ? 'Made under a pre-arranged Rule 10b5-1 trading plan'
                : 'Not marked as a Rule 10b5-1 trading plan'}
            </dd>
          </>
        )}
      </dl>
    </section>
  )
}
