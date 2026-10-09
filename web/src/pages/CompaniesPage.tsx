import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router'
import { watchlistQuery } from '../api/queries'
import { ErrorMessage, Loading } from '../components/QueryStatus'
import styles from './CompaniesPage.module.css'
import { usePageTitle } from '../lib/usePageTitle'

export function CompaniesPage() {
  const query = useQuery(watchlistQuery())
  const [filter, setFilter] = useState('')
  usePageTitle('Companies')

  const needle = filter.trim().toLowerCase()
  const companies = (query.data?.items ?? []).filter(
    (company) =>
      company.ticker.toLowerCase().includes(needle) ||
      company.name.toLowerCase().includes(needle),
  )

  return (
    <>
      <h1>Companies</h1>
      <p className="muted">
        The watchlist: every new filing from these companies is collected and
        summarized.
      </p>
      <p>
        <label htmlFor="company-filter" className="visually-hidden">
          Filter companies
        </label>
        <input
          id="company-filter"
          type="search"
          placeholder="Filter by name or ticker"
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
        />
      </p>
      {/* Tells screen readers how many companies the filter left, as they type. */}
      <p role="status" className="visually-hidden">
        {needle && query.data
          ? `${companies.length} of ${query.data.items.length} companies match.`
          : ''}
      </p>
      {query.isPending ? (
        <Loading />
      ) : query.isError ? (
        <ErrorMessage error={query.error} />
      ) : companies.length === 0 ? (
        <p className="muted">No companies match “{filter}”.</p>
      ) : (
        <ul className={styles.grid}>
          {companies.map((company) => (
            <li key={company.cik}>
              <Link to={`/companies/${company.ticker}`} className={styles.card}>
                <span className={styles.ticker}>{company.ticker}</span>
                <span>{company.name}</span>
                {company.exchange && (
                  <span className="muted">{company.exchange}</span>
                )}
              </Link>
            </li>
          ))}
        </ul>
      )}
    </>
  )
}
