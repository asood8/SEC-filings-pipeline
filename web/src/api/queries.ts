import {
  infiniteQueryOptions,
  keepPreviousData,
  queryOptions,
} from '@tanstack/react-query'
import { api, unwrap, type TradeSide } from './client'

const PAGE_SIZE = 25

export type FilingFilters = { formType?: string; cik?: number }

export const filingsQuery = ({ formType, cik }: FilingFilters) =>
  infiniteQueryOptions({
    queryKey: ['filings', { formType, cik }],
    queryFn: ({ pageParam }) =>
      unwrap(
        api.GET('/filings', {
          params: {
            query: {
              form_type: formType,
              cik,
              cursor: pageParam,
              limit: PAGE_SIZE,
            },
          },
        }),
      ),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
  })

export const filingQuery = (accessionNo: string) =>
  queryOptions({
    queryKey: ['filing', accessionNo],
    queryFn: () =>
      unwrap(
        api.GET('/filings/{accession_no}', {
          params: { path: { accession_no: accessionNo } },
        }),
      ),
  })

export const watchlistQuery = () =>
  queryOptions({
    queryKey: ['companies', 'watchlist'],
    queryFn: () =>
      unwrap(
        api.GET('/companies', {
          params: { query: { watchlist: true, limit: 200 } },
        }),
      ),
  })

export const companyQuery = (ticker: string) =>
  queryOptions({
    queryKey: ['company', ticker.toUpperCase()],
    queryFn: () =>
      unwrap(api.GET('/companies/{ticker}', { params: { path: { ticker } } })),
  })

export const insiderActivityQuery = (ticker: string, days: number) =>
  queryOptions({
    queryKey: ['insider-activity', ticker.toUpperCase(), days],
    queryFn: () =>
      unwrap(
        api.GET('/companies/{ticker}/insider-activity', {
          params: { path: { ticker }, query: { days } },
        }),
      ),
  })

export const insiderTradesQuery = (side: TradeSide, days: number) =>
  queryOptions({
    queryKey: ['insider-trades', side, days],
    queryFn: () =>
      unwrap(api.GET('/insider-trades', { params: { query: { side, days } } })),
  })

export const callsQuery = (days: number) =>
  queryOptions({
    queryKey: ['calls', days],
    queryFn: () => unwrap(api.GET('/calls', { params: { query: { days } } })),
  })

export type SearchMode = 'hybrid' | 'keyword' | 'semantic'
export type SearchParams = {
  q: string
  mode: SearchMode
  formType?: string
  /** Only filings filed on or after this date (YYYY-MM-DD). */
  since?: string
}

/** The most results one search returns. */
export const SEARCH_LIMIT = 30

export const searchQuery = ({ q, mode, formType, since }: SearchParams) =>
  queryOptions({
    queryKey: ['search', { q, mode, formType, since }],
    queryFn: () =>
      unwrap(
        api.GET('/search', {
          params: {
            query: { q, mode, form_type: formType, since, limit: SEARCH_LIMIT },
          },
        }),
      ),
    enabled: q.trim().length > 0,
    // Keep showing the previous results while a new search loads.
    placeholderData: keepPreviousData,
  })
