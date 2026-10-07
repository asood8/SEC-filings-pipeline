import { useInfiniteQuery, useQuery } from '@tanstack/react-query'
import { useParams } from 'react-router'
import { isNotFound } from '../api/client'
import { companyQuery, filingsQuery } from '../api/queries'
import { FilingList } from '../components/FilingList'
import { ErrorMessage, Loading } from '../components/QueryStatus'
import { edgarCompanyUrl } from '../lib/format'
import { NotFoundPage } from './NotFoundPage'

export function CompanyPage() {
  const { ticker = '' } = useParams()
  const company = useQuery(companyQuery(ticker))
  const filings = useInfiniteQuery({
    ...filingsQuery({ cik: company.data?.cik }),
    enabled: company.data !== undefined,
  })

  if (company.isPending) return <Loading />
  if (isNotFound(company.error)) return <NotFoundPage what="company" />
  if (company.isError) return <ErrorMessage error={company.error} />

  const { name, cik, exchange } = company.data
  return (
    <>
      <title>{`${name} (${company.data.ticker}) · SEC Filings`}</title>
      <h1>{name}</h1>
      <p className="muted">
        {company.data.ticker}
        {exchange && ` · ${exchange}`} · CIK {cik} ·{' '}
        <a href={edgarCompanyUrl(cik)} target="_blank" rel="noreferrer">
          All filings on EDGAR ↗
        </a>
      </p>
      <h2>Recent filings</h2>
      {filings.isPending ? (
        <Loading />
      ) : filings.isError ? (
        <ErrorMessage error={filings.error} />
      ) : (
        <>
          <FilingList
            filings={filings.data.pages.flatMap((page) => page.items)}
            showCompany={false}
          />
          {filings.data.pages[0].items.length === 0 && (
            <p className="muted">No filings collected yet.</p>
          )}
          {filings.hasNextPage && (
            <p>
              <button
                type="button"
                onClick={() => filings.fetchNextPage()}
                disabled={filings.isFetchingNextPage}
              >
                {filings.isFetchingNextPage ? 'Loading…' : 'Load more'}
              </button>
            </p>
          )}
        </>
      )}
    </>
  )
}
