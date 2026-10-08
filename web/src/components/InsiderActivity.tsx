import { useQuery } from '@tanstack/react-query'
import type { TradeTotals } from '../api/client'
import { insiderActivityQuery } from '../api/queries'
import { formatDate, formatMoney, formatShares } from '../lib/format'
import styles from './DataTable.module.css'
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
          <div className={styles.scroll}>
            <table className={styles.table}>
              <caption className="visually-hidden">
                Insider purchases and sales by owner, last {DAYS} days
              </caption>
              <thead>
                <tr>
                  <th scope="col">Reported by</th>
                  <th scope="col" className={styles.number}>
                    Bought
                  </th>
                  <th scope="col" className={styles.number}>
                    Sold
                  </th>
                  <th scope="col" className={styles.number}>
                    Filings
                  </th>
                  <th scope="col" className={styles.number}>
                    Latest
                  </th>
                </tr>
              </thead>
              <tbody>
                {query.data.owners.map((owner) => (
                  <tr key={owner.reporting_owners.join('|')}>
                    <td>{owner.reporting_owners.join('; ')}</td>
                    <td data-label="Bought" className={styles.number}>
                      <Trades totals={owner.bought} />
                    </td>
                    <td data-label="Sold" className={styles.number}>
                      <Trades totals={owner.sold} />
                    </td>
                    <td data-label="Filings" className={styles.number}>
                      {owner.filings}
                    </td>
                    <td data-label="Latest" className={styles.number}>
                      <time dateTime={owner.latest_date}>
                        {formatDate(owner.latest_date)}
                      </time>
                    </td>
                  </tr>
                ))}
              </tbody>
              <tfoot>
                <tr>
                  <td>All insiders</td>
                  <td data-label="Bought" className={styles.number}>
                    <Trades totals={query.data.bought} />
                  </td>
                  <td data-label="Sold" className={styles.number}>
                    <Trades totals={query.data.sold} />
                  </td>
                  <td data-label="Filings" className={styles.number}>
                    {query.data.filings}
                  </td>
                  <td className={styles.number} />
                </tr>
              </tfoot>
            </table>
          </div>
          {[query.data.bought, query.data.sold].some(
            (t) => t.unpriced_shares > 0,
          ) && (
            <p className={styles.note}>
              * Includes shares whose price the filing gives only in a footnote;
              their value isn't counted.
            </p>
          )}
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
