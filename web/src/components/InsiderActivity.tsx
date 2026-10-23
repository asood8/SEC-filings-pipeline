import { useQuery } from '@tanstack/react-query'
import type { InsiderActivity as Activity, TradeTotals } from '../api/client'
import { insiderActivityQuery } from '../api/queries'
import { formatDate, formatMoney, formatShares } from '../lib/format'
import { DataTable } from './DataTable'
import styles from './DataTable.module.css'
import { OwnerNames } from './OwnerNames'
import { ErrorMessage, Loading } from './QueryStatus'

const DAYS = 90

/** Insiders' open-market buying and selling of a company's stock, by owner. */
export function InsiderActivity({ ticker }: { ticker: string }) {
  const query = useQuery(insiderActivityQuery(ticker, DAYS))
  return (
    <section aria-labelledby="insider-activity">
      <h2 id="insider-activity">Insider buying and selling</h2>
      {query.isPending ? (
        <Loading label="Loading insider trades…" />
      ) : query.isError ? (
        <ErrorMessage error={query.error} />
      ) : query.data.owners.length === 0 ? (
        <p className="muted">
          No insider purchases or sales of {ticker} stock reported in the last{' '}
          {DAYS} days.
        </p>
      ) : (
        <>
          <p className="muted">
            Purchases and sales of {ticker} stock reported on Form 4s since{' '}
            {formatDate(query.data.since)}. Awards, option exercises, tax
            withholding, and gifts aren't included.
          </p>
          <DataTable
            caption={`Insider purchases and sales by owner, last ${DAYS} days`}
          >
            <thead role="rowgroup">
              <tr role="row">
                <th scope="col" role="columnheader">
                  Reported by
                </th>
                <th scope="col" role="columnheader" className={styles.number}>
                  Bought
                </th>
                <th scope="col" role="columnheader" className={styles.number}>
                  Sold
                </th>
                <th scope="col" role="columnheader" className={styles.number}>
                  Filings
                </th>
                <th scope="col" role="columnheader" className={styles.number}>
                  Latest
                </th>
              </tr>
            </thead>
            <tbody role="rowgroup">
              {query.data.owners.map((owner) => (
                <tr
                  key={owner.reporting_owners
                    .map((o) => o.cik ?? o.name)
                    .join('|')}
                  role="row"
                >
                  <th scope="row" role="rowheader">
                    <OwnerNames owners={owner.reporting_owners} />
                  </th>
                  <td role="cell" data-label="Bought" className={styles.number}>
                    <Trades totals={owner.bought} />
                  </td>
                  <td role="cell" data-label="Sold" className={styles.number}>
                    <Trades totals={owner.sold} />
                  </td>
                  <td
                    role="cell"
                    data-label="Filings"
                    className={styles.number}
                  >
                    {owner.filings}
                  </td>
                  <td role="cell" data-label="Latest" className={styles.number}>
                    <time dateTime={owner.latest_date}>
                      {formatDate(owner.latest_date)}
                    </time>
                  </td>
                </tr>
              ))}
            </tbody>
            <tfoot role="rowgroup">
              <tr role="row">
                <th scope="row" role="rowheader">
                  All insiders
                </th>
                <td role="cell" data-label="Bought" className={styles.number}>
                  <Trades totals={query.data.bought} />
                </td>
                <td role="cell" data-label="Sold" className={styles.number}>
                  <Trades totals={query.data.sold} />
                </td>
                <td role="cell" data-label="Filings" className={styles.number}>
                  {query.data.filings}
                </td>
                <td role="cell" className={styles.number} />
              </tr>
            </tfoot>
          </DataTable>
          {[query.data.bought, query.data.sold].some(
            (t) => t.unpriced_shares > 0,
          ) && (
            <p className={styles.note}>
              * Includes shares whose price the filing gives only in a footnote;
              their value isn't counted.
            </p>
          )}
          <Planned activity={query.data} />
        </>
      )}
    </section>
  )
}

/** "$144,150" over "300 shares", or a dash for none. */
function Trades({ totals }: { totals: TradeTotals }) {
  if (totals.shares === 0) return <span className="muted">—</span>
  return (
    <span>
      {formatMoney(totals.value)}
      {totals.unpriced_shares > 0 && '*'}{' '}
      <span className={styles.detail}>
        {formatShares(totals.shares)} shares
      </span>
    </span>
  )
}

const shareFormat = new Intl.NumberFormat('en-US', { style: 'percent' })

/** How much of the buying and selling ran under 10b5-1 plans, by shares; nothing when none did.
 * "Under 10b5-1 plans, set up in advance: 94% of the shares sold." */
function Planned({ activity }: { activity: Activity }) {
  const parts = [
    ['sold', activity.sold, activity.sold_planned],
    ['bought', activity.bought, activity.bought_planned],
  ] as const
  const shares = parts
    .filter(([, , planned]) => planned.shares > 0)
    .map(
      ([verb, all, planned]) =>
        `${shareFormat.format(planned.shares / all.shares)} of the shares ${verb}`,
    )
  if (shares.length === 0) return null
  return (
    <p className={styles.note}>
      Under 10b5-1 plans, set up in advance: {shares.join(', and ')}.
    </p>
  )
}
