import { useEffect, useRef } from 'react'
import type { Filing } from '../api/client'
import { FilingList } from './FilingList'
import { ErrorMessage } from './QueryStatus'

type Props = {
  /** The pages loaded so far, from an infinite query. */
  pages: { items: Filing[] }[]
  hasNextPage: boolean
  isFetchingNextPage: boolean
  fetchNextPage: () => Promise<{ isError: boolean }>
  /** A failed "Load more" (or refresh): shown under the filings already loaded. */
  error: Error | null
  /** Shown when there are no filings at all. */
  empty: string
  showCompany?: boolean
}

/**
 * A list of filings with a "Load more" button. Once the next page arrives, focus moves to its
 * first filing, so keyboard and screen reader users carry on reading where the new filings
 * start (and focus isn't lost when the button disappears after the last page).
 */
export function PagedFilingList({
  pages,
  hasNextPage,
  isFetchingNextPage,
  fetchNextPage,
  error,
  empty,
  showCompany,
}: Props) {
  const filings = pages.flatMap((page) => page.items)
  const list = useRef<HTMLDivElement>(null)
  const focusIndex = useRef<number>(undefined)

  useEffect(() => {
    const index = focusIndex.current
    if (index === undefined) return
    const link = list.current?.querySelectorAll('li')[index]?.querySelector('a')
    if (!link) return
    focusIndex.current = undefined
    link.focus()
  })

  function loadMore() {
    // aria-disabled rather than disabled while loading, so the button keeps focus.
    if (isFetchingNextPage) return
    focusIndex.current = filings.length
    fetchNextPage().then((result) => {
      if (result.isError) focusIndex.current = undefined
    })
  }

  return (
    <>
      <div ref={list}>
        <FilingList filings={filings} showCompany={showCompany} />
      </div>
      {filings.length === 0 && <p className="muted">{empty}</p>}
      {error && <ErrorMessage error={error} />}
      {hasNextPage && (
        <p>
          <button
            type="button"
            onClick={loadMore}
            aria-disabled={isFetchingNextPage || undefined}
          >
            {isFetchingNextPage ? 'Loading…' : 'Load more'}
          </button>
        </p>
      )}
    </>
  )
}
