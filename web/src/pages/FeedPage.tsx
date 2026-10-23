import { useInfiniteQuery } from '@tanstack/react-query'
import { useSearchParams } from 'react-router'
import { filingsFeedUrl } from '../api/client'
import { filingsQuery } from '../api/queries'
import { ExternalLink } from '../components/ExternalLink'
import { PagedFilingList } from '../components/PagedFilingList'
import { ErrorMessage, Loading } from '../components/QueryStatus'
import { ALERT_8K_ITEMS } from '../lib/alertFilters'
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
  // Only 8-Ks report items; the item list is the one alerts offer.
  const item =
    formType === '8-K' &&
    ALERT_8K_ITEMS.some(({ item }) => item === params.get('item'))
      ? params.get('item')!
      : ''
  const filters = {
    formType: formType || undefined,
    item: item || undefined,
  }
  const query = useInfiniteQuery(filingsQuery(filters))
  usePageTitle('Latest filings')
  const feed = filingsFeedUrl(filters)

  function setForm(value: string) {
    setParams(value ? { form: value } : {}, { replace: true })
  }

  function setItem(value: string) {
    setParams(value ? { form: '8-K', item: value } : { form: '8-K' }, {
      replace: true,
    })
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
          Follow new summaries{formType && ' matching this filter'} in a feed
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
      {formType === '8-K' && (
        <p>
          <label className={styles.item}>
            What the 8-K reports{' '}
            <select
              value={item}
              onChange={(event) => setItem(event.target.value)}
            >
              <option value="">Any item</option>
              {ALERT_8K_ITEMS.map(({ item, label }) => (
                <option key={item} value={item}>
                  {item} {label}
                </option>
              ))}
            </select>
          </label>
        </p>
      )}

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
