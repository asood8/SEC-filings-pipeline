import { useQuery } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router'
import type { InsiderTrade, TradeSide } from '../api/client'
import { insiderTradesQuery } from '../api/queries'
import { DataTable } from '../components/DataTable'
import table from '../components/DataTable.module.css'
import { OwnerNames } from '../components/OwnerNames'
import { ErrorMessage, Loading } from '../components/QueryStatus'
import { formatDate, formatMoney, formatShares } from '../lib/format'
import filters from './FeedPage.module.css'
import styles from './InsiderTradesPage.module.css'
import { usePageTitle } from '../lib/usePageTitle'

const SIDES: { value: TradeSide; label: string; noun: string }[] = [
  { value: 'buy', label: 'Purchases', noun: 'purchases' },
  { value: 'sell', label: 'Sales', noun: 'sales' },
]
const PERIODS = [30, 90]
const DEFAULT_SIDE: TradeSide = 'buy'
const DEFAULT_DAYS = 30
const NONE = '—'

/** The largest recent insider purchases or sales across all companies. The choices live in the
 * URL (?side=sell&days=90&plans=hide), so every view has a link; defaults are left out. */
export function InsiderTradesPage() {
  const [params, setParams] = useSearchParams()
  const side: TradeSide = params.get('side') === 'sell' ? 'sell' : DEFAULT_SIDE
  const period =
    PERIODS.find((days) => String(days) === params.get('days')) ?? DEFAULT_DAYS
  const hidePlans = params.get('plans') === 'hide'
  const query = useQuery(insiderTradesQuery(side, period, !hidePlans))
  const noun = SIDES.find((s) => s.value === side)?.noun
  const without = hidePlans ? ', without 10b5-1 plan trades' : ''
  usePageTitle(`Insider trades: ${noun}, last ${period} days`)

  function choose(next: {
    side?: TradeSide
    days?: number
    hidePlans?: boolean
  }) {
    const chosen = {
      side: next.side ?? side,
      days: next.days ?? period,
      hidePlans: next.hidePlans ?? hidePlans,
    }
    const search: Record<string, string> = {}
    if (chosen.side !== DEFAULT_SIDE) search.side = chosen.side
    if (chosen.days !== DEFAULT_DAYS) search.days = String(chosen.days)
    if (chosen.hidePlans) search.plans = 'hide'
    setParams(search, { replace: true })
  }

  return (
    <>
      <h1>Insider trades</h1>
      <p className="muted">
        The largest recent purchases and sales of company stock by insiders
        (officers, directors, and major shareholders), from the Form 4s
        collected for the watchlist.
      </p>
      <div className={styles.controls}>
        <fieldset className={filters.filters}>
          <legend className="visually-hidden">Trades</legend>
          {SIDES.map((option) => (
            <label key={option.value} className={filters.filter}>
              <input
                type="radio"
                name="side"
                value={option.value}
                checked={side === option.value}
                onChange={() => choose({ side: option.value })}
              />
              {option.label}
            </label>
          ))}
        </fieldset>
        <fieldset className={filters.filters}>
          <legend className="visually-hidden">Period</legend>
          {PERIODS.map((option) => (
            <label key={option} className={filters.filter}>
              <input
                type="radio"
                name="days"
                value={option}
                checked={period === option}
                onChange={() => choose({ days: option })}
              />
              Last {option} days
            </label>
          ))}
        </fieldset>
        <label className={filters.filter}>
          <input
            type="checkbox"
            checked={hidePlans}
            onChange={(event) => choose({ hidePlans: event.target.checked })}
          />
          Leave out 10b5-1 plan trades
        </label>
      </div>
      <p className={table.note}>
        Only open-market (or private) purchases and sales of a company's stock
        count. Awards, option exercises, shares withheld for taxes, and gifts
        are left out: they aren't decisions to buy or sell. A 10b5-1 plan is a
        trading plan the insider set up in advance, which then trades on its own
        schedule, so its trades say less about what the insider thinks now.
      </p>

      {query.isPending ? (
        <Loading label="Loading insider trades…" />
      ) : query.isError ? (
        <ErrorMessage error={query.error} />
      ) : query.data.items.length === 0 ? (
        <p className="muted">
          No insider {noun} reported in the last {period} days{without}.
        </p>
      ) : (
        <>
          <p className="muted">
            Trades since {formatDate(query.data.since)}, largest first; one row
            per filing.
          </p>
          <DataTable
            caption={`Largest insider ${noun}, last ${period} days${without}`}
          >
            <thead role="rowgroup">
              <tr role="row">
                <th scope="col" role="columnheader">
                  Stock
                </th>
                <th scope="col" role="columnheader">
                  Insider
                </th>
                <th scope="col" role="columnheader" className={table.number}>
                  Value
                </th>
                <th scope="col" role="columnheader" className={table.number}>
                  Average price
                </th>
                <th scope="col" role="columnheader" className={table.number}>
                  Traded
                </th>
                <th scope="col" role="columnheader">
                  10b5-1 plan
                </th>
                <th scope="col" role="columnheader">
                  Filing
                </th>
              </tr>
            </thead>
            <tbody role="rowgroup">
              {query.data.items.map((trade) => (
                <Row key={trade.accession_no} trade={trade} />
              ))}
            </tbody>
          </DataTable>
          {query.data.items.some((t) => t.unpriced_shares > 0) && (
            <p className={table.note}>
              * Includes shares whose price the filing gives only in a footnote;
              their value isn't counted.
            </p>
          )}
        </>
      )}
    </>
  )
}

function Row({ trade }: { trade: InsiderTrade }) {
  const { issuer } = trade
  return (
    <tr role="row">
      <th scope="row" role="rowheader">
        <span>
          {issuer.ticker ? (
            <>
              <Link to={`/companies/${issuer.ticker}`}>{issuer.ticker}</Link>{' '}
              <span className={table.detail}>{issuer.name}</span>
            </>
          ) : (
            issuer.name
          )}
        </span>
      </th>
      <td role="cell" data-label="Insider">
        <span>
          <OwnerNames owners={trade.reporting_owners} />
        </span>
      </td>
      <td role="cell" data-label="Value" className={table.number}>
        <span>
          {trade.average_price !== null ? formatMoney(trade.value) : NONE}
          {trade.unpriced_shares > 0 && '*'}{' '}
          <span className={table.detail}>
            {formatShares(trade.shares)} shares
          </span>
        </span>
      </td>
      <td role="cell" data-label="Average price" className={table.number}>
        {trade.average_price !== null ? formatMoney(trade.average_price) : NONE}
      </td>
      <td role="cell" data-label="Traded" className={table.number}>
        <time dateTime={trade.transaction_date}>
          {formatDate(trade.transaction_date)}
        </time>
      </td>
      <td role="cell" data-label="10b5-1 plan">
        {trade.is_10b5_1 === null
          ? 'Not stated'
          : trade.is_10b5_1
            ? 'Yes'
            : 'No'}
      </td>
      <td role="cell" data-label="Filing">
        <Link to={`/filings/${trade.accession_no}`}>
          Filed {formatDate(trade.filed_at)}{' '}
          {/* Which filing the link opens, for a list of links out of context. */}
          <span className="visually-hidden">
            ({issuer.ticker ?? issuer.name} Form 4)
          </span>
        </Link>
      </td>
    </tr>
  )
}
