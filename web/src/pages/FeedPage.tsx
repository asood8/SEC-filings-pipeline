import { useInfiniteQuery } from '@tanstack/react-query'
import { useSearchParams } from 'react-router'
import { filingsQuery } from '../api/queries'
import { PagedFilingList } from '../components/PagedFilingList'
import { ErrorMessage, Loading } from '../components/QueryStatus'
import styles from './FeedPage.module.css'

const FORM_FILTERS = [
  { value: '', label: 'All' },
  { value: '4', label: 'Insider trades (Form 4)' },
  { value: '8-K', label: 'Current reports (8-K)' },
]

export function FeedPage() {
  const [params, setParams] = useSearchParams()
  const formType = params.get('form') ?? ''
  const query = useInfiniteQuery(
    filingsQuery({ formType: formType || undefined }),
  )

  function setForm(value: string) {
    setParams(value ? { form: value } : {}, { replace: true })
  }

  return (
    <>
      <title>Latest filings · SEC Filings</title>
      <h1>Latest filings</h1>
      <p className="muted">
        New SEC filings from a watchlist of well-known public companies, with
        plain-English summaries.
      </p>
      <fieldset className={styles.filters}>
        <legend className="visually-hidden">Form type</legend>
        {FORM_FILTERS.map((filter) => (
          <label key={filter.value} className={styles.filter}>
            <input
              type="radio"
              name="form"
              value={filter.value}
              checked={formType === filter.value}
              onChange={() => setForm(filter.value)}
            />
            {filter.label}
          </label>
        ))}
      </fieldset>

      {query.data ? (
        <PagedFilingList
          pages={query.data.pages}
          hasNextPage={query.hasNextPage}
          isFetchingNextPage={query.isFetchingNextPage}
          fetchNextPage={query.fetchNextPage}
          error={query.error}
          empty="No filings yet."
        />
      ) : query.isError ? (
        <ErrorMessage error={query.error} />
      ) : (
        <Loading />
      )}
    </>
  )
}
