import { useInfiniteQuery } from '@tanstack/react-query'
import { useSearchParams } from 'react-router'
import { filingsFeedUrl } from '../api/client'
import { filingsQuery } from '../api/queries'
import { ExternalLink } from '../components/ExternalLink'
import { PagedFilingList } from '../components/PagedFilingList'
import { ErrorMessage, Loading } from '../components/QueryStatus'
import styles from './FeedPage.module.css'
import { usePageTitle } from '../lib/usePageTitle'

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
  usePageTitle('Latest filings')
  const feed = filingsFeedUrl({ formType: formType || undefined })

  function setForm(value: string) {
    setParams(value ? { form: value } : {}, { replace: true })
  }

  return (
    <>
      <h1>Latest filings</h1>
      <p className="muted">
        New SEC filings from a watchlist of well-known public companies, with
        plain-English summaries.
      </p>
      <p>
        <ExternalLink href={feed}>Atom feed</ExternalLink>{' '}
        <span className="muted">
          Follow new summaries{formType && ' of this form type'} in a feed
          reader or Slack's RSS app by pasting {feed}
        </span>
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
