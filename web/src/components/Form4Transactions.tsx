import { Link } from 'react-router'
import type { Form4Transaction } from '../api/client'
import {
  formatDate,
  formatMoney,
  formatPrice,
  formatShares,
} from '../lib/format'
import { TRANSACTION_CODES } from '../lib/glossary'
import { DataTable } from './DataTable'
import styles from './DataTable.module.css'

const NONE = '—'

/** A Form 4's transaction lines: stock first, then derivatives, as filed. Lines a later
 * Form 4/A restates say so and link it. */
export function Form4Transactions({
  transactions,
}: {
  transactions: Form4Transaction[]
}) {
  const footnotePrices = transactions.some((t) => t.price_per_share === null)
  const restated = transactions.some((t) => t.superseded_by !== null)
  return (
    <>
      <DataTable caption="Transactions reported in this filing">
        <thead role="rowgroup">
          <tr role="row">
            <th scope="col" role="columnheader">
              Transaction
            </th>
            <th scope="col" role="columnheader">
              Date
            </th>
            <th scope="col" role="columnheader">
              Security
            </th>
            <th scope="col" role="columnheader" className={styles.number}>
              Shares
            </th>
            <th scope="col" role="columnheader" className={styles.number}>
              Price
            </th>
            <th scope="col" role="columnheader" className={styles.number}>
              Value
            </th>
            <th scope="col" role="columnheader" className={styles.number}>
              Owned after
            </th>
            <th scope="col" role="columnheader">
              Ownership
            </th>
          </tr>
        </thead>
        <tbody role="rowgroup">
          {transactions.map((t) => (
            <Row key={t.line} transaction={t} />
          ))}
        </tbody>
      </DataTable>
      {footnotePrices && (
        <p className={styles.note}>
          {NONE} in Price: the filing gives the price only in a footnote. The
          original on sec.gov has it.
        </p>
      )}
      {restated && (
        <p className={styles.note}>
          Restated lines: an amendment filed later reports them again, corrected
          or unchanged. Insider totals count the amendment&apos;s lines instead.
        </p>
      )}
    </>
  )
}

function Row({ transaction: t }: { transaction: Form4Transaction }) {
  const value =
    t.shares !== null && t.price_per_share !== null
      ? t.shares * t.price_per_share
      : null
  return (
    <tr
      role="row"
      className={t.superseded_by !== null ? styles.superseded : undefined}
    >
      <th scope="row" role="rowheader">
        {transactionType(t.code)}
        {t.superseded_by !== null && (
          <span className={styles.detail}>
            Restated by{' '}
            <Link to={`/filings/${t.superseded_by}`}>the amendment</Link>
          </span>
        )}
      </th>
      <td role="cell" data-label="Date">
        {t.transaction_date ? (
          <time dateTime={t.transaction_date}>
            {formatDate(t.transaction_date)}
          </time>
        ) : (
          NONE
        )}
      </td>
      <td role="cell" data-label="Security">
        <span>
          {t.security_title}
          {t.is_derivative && (
            <>
              {' '}
              <span className="badge">Derivative</span>
            </>
          )}
        </span>
      </td>
      <td role="cell" data-label="Shares" className={styles.number}>
        <Shares shares={t.shares} direction={t.acquired_disposed} />
      </td>
      <td role="cell" data-label="Price" className={styles.number}>
        {t.price_per_share !== null ? formatPrice(t.price_per_share) : NONE}
      </td>
      <td role="cell" data-label="Value" className={styles.number}>
        {value !== null ? formatMoney(value) : NONE}
      </td>
      <td role="cell" data-label="Owned after" className={styles.number}>
        {t.shares_owned_after !== null
          ? formatShares(t.shares_owned_after)
          : NONE}
      </td>
      <td role="cell" data-label="Ownership">
        {t.direct_or_indirect === 'D'
          ? 'Direct'
          : t.direct_or_indirect === 'I'
            ? 'Indirect'
            : NONE}
      </td>
    </tr>
  )
}

function transactionType(code: string | null): string {
  if (!code) return 'Type not given'
  return TRANSACTION_CODES[code] ?? `SEC code ${code}`
}

/** "+720" acquired, "−720" disposed, spelled out for screen readers. */
function Shares({
  shares,
  direction,
}: {
  shares: number | null
  direction: Form4Transaction['acquired_disposed']
}) {
  if (shares === null) return NONE
  const word =
    direction === 'A' ? 'acquired' : direction === 'D' ? 'disposed of' : null
  const sign = direction === 'A' ? '+' : direction === 'D' ? '−' : ''
  return (
    <span title={word ? `Shares ${word}` : undefined}>
      {word && <span className="visually-hidden">{word} </span>}
      <span aria-hidden={word ? true : undefined}>{sign}</span>
      {formatShares(shares)}
    </span>
  )
}
