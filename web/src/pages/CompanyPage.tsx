import { useInfiniteQuery, useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router'
import { API_BASE_URL, filingsFeedUrl, isNotFound } from '../api/client'
import { callsQuery, companyQuery, filingsQuery } from '../api/queries'
import { PagedFilingList } from '../components/PagedFilingList'
import { ExternalLink } from '../components/ExternalLink'
import { InsiderActivity } from '../components/InsiderActivity'
import { ErrorMessage, Loading } from '../components/QueryStatus'
import { edgarCompanyUrl, formatDate, formatEasternTime } from '../lib/format'
import { NotFoundPage } from './NotFoundPage'
import { usePageTitle } from '../lib/usePageTitle'

export function CompanyPage() {
  const { ticker = '' } = useParams()
  const company = useQuery(companyQuery(ticker))
  const filings = useInfiniteQuery({
    ...filingsQuery({ cik: company.data?.cik }),
    enabled: company.data !== undefined,
  })
  usePageTitle(
    company.data
      ? `${company.data.name} (${company.data.ticker})`
      : isNotFound(company.error)
        ? 'Company not found'
        : ticker.toUpperCase(),
  )

  if (company.isPending) return <Loading />
  if (isNotFound(company.error)) return <NotFoundPage what="company" />
  if (company.isError) return <ErrorMessage error={company.error} />

  const { name, cik, exchange, industry } = company.data
  return (
    <>
      <h1>{name}</h1>
      <p className="muted">
        {company.data.ticker}
        {exchange && ` · ${exchange}`}
        {industry && ` · ${industry}`} · CIK {cik} ·{' '}
        <ExternalLink href={edgarCompanyUrl(cik)}>
          All filings on EDGAR
        </ExternalLink>
      </p>
      <p>
        <Link to={`/alerts?ticker=${company.data.ticker}`}>
          Get alerts for {company.data.ticker}
        </Link>{' '}
        ·{' '}
        <ExternalLink href={filingsFeedUrl({ cik })}>
          Atom feed of {company.data.ticker} filings
        </ExternalLink>
      </p>
      <NextCall ticker={company.data.ticker} />
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

/** The next investor call the company's 8-Ks announced, if any (the parser looks up to 120 days
 * ahead), with its calendar feed. Nothing while loading or when none is announced. */
function NextCall({ ticker }: { ticker: string }) {
  const calls = useQuery(callsQuery(120, ticker))
  const next = calls.data?.items[0]
  if (!next) return null
  const url = next.webcast_url
  const feed = `${API_BASE_URL}/calls.ics?ticker=${encodeURIComponent(ticker)}`
  return (
    <p>
      <strong>Next call:</strong> {next.kind},{' '}
      {next.starts_at ? (
        <time dateTime={next.starts_at}>
          {formatEasternTime(next.starts_at)}
        </time>
      ) : (
        <time dateTime={next.day}>{formatDate(next.day)}</time>
      )}
      {url && /^https?:\/\//.test(url) && (
        <>
          {' '}
          · <ExternalLink href={url}>Webcast</ExternalLink>
        </>
      )}{' '}
      · <Link to={`/filings/${next.accession_no}`}>Announcement</Link> ·{' '}
      <ExternalLink href={feed}>Calendar feed of {ticker} calls</ExternalLink>
    </p>
  )
}
