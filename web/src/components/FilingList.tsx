import { Link } from 'react-router'
import type { Filing } from '../api/client'
import { formatDate, formLabel } from '../lib/format'
import styles from './FilingList.module.css'

type Props = {
  filings: Filing[]
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
