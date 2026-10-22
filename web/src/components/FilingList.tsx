import { Link } from 'react-router'
import type { Filing, SearchResult } from '../api/client'
import { formatDate, formLabel } from '../lib/format'
import styles from './FilingList.module.css'

type Props = {
  /** Search results also carry a snippet of their summary, shown under the headline. */
  filings: (Filing & Partial<Pick<SearchResult, 'snippet'>>)[]
  /** Hide the company on a company's own page. */
  showCompany?: boolean
}

export function FilingList({ filings, showCompany = true }: Props) {
  return (
    <ul className={styles.list}>
      {filings.map((filing) => (
        <li key={filing.accession_no} className={styles.item}>
          <Link
            to={`/filings/${filing.accession_no}`}
            className={styles.headline}
          >
            {filing.headline ?? (
              <span className={styles.pending}>
                {formLabel(filing.form_type)} filing, summary pending
              </span>
            )}
          </Link>
          {filing.snippet && filing.snippet.length > 0 && (
            <p className={styles.snippet}>
              {filing.snippet.map((part, i) =>
                part.match ? <mark key={i}>{part.text}</mark> : part.text,
              )}
            </p>
          )}
          <div className={styles.meta}>
            {showCompany && (
              <>
                <Link
                  to={`/companies/${filing.ticker}`}
                  className={styles.ticker}
                >
                  {filing.ticker}
                </Link>
                <span>{filing.company_name}</span>
              </>
            )}
            <span className="badge">{formLabel(filing.form_type)}</span>
            <time dateTime={filing.filed_at}>
              {formatDate(filing.filed_at)}
            </time>
          </div>
        </li>
      ))}
    </ul>
  )
}
