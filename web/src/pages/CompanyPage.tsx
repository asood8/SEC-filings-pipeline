import { useInfiniteQuery, useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router'
import { isNotFound } from '../api/client'
import { companyQuery, filingsQuery } from '../api/queries'
import { PagedFilingList } from '../components/PagedFilingList'
import { ExternalLink } from '../components/ExternalLink'
import { InsiderActivity } from '../components/InsiderActivity'
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
        <ExternalLink href={edgarCompanyUrl(cik)}>
          All filings on EDGAR
        </ExternalLink>
      </p>
      <p>
        <Link to={`/alerts?ticker=${company.data.ticker}`}>
          Get alerts for {company.data.ticker}
        </Link>
      </p>
      <InsiderActivity ticker={company.data.ticker} />
      <h2>Recent filings</h2>
      {filings.data ? (
        <PagedFilingList
          pages={filings.data.pages}
          hasNextPage={filings.hasNextPage}
          isFetchingNextPage={filings.isFetchingNextPage}
          fetchNextPage={filings.fetchNextPage}
          error={filings.error}
          empty="No filings collected yet."
          showCompany={false}
        />
      ) : filings.isError ? (
        <ErrorMessage error={filings.error} />
      ) : (
        <Loading />
      )}
    </>
  )
}
