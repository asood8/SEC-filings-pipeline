import type { Form4Trades } from '../lib/form4Filters'

const TRADE_CHOICES: { value: Form4Trades; label: string }[] = [
  { value: 'all', label: 'Every Form 4' },
  { value: 'open_market', label: 'Open-market buys and sells only' },
  { value: 'purchases', label: 'Open-market purchases only' },
]

/** The Form 4 filters' fields (filings_common/alert_filters.py), for a create or edit form. */
export function Form4FilterFields({
  trades = 'all',
  minValue = null,
}: {
  trades?: Form4Trades
  minValue?: number | null
}) {
  return (
    <>
      <label>
        Insider trades (Form 4)
        <select name="form4_trades" defaultValue={trades}>
          {TRADE_CHOICES.map(({ value, label }) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
      </label>
      <label>
        Only when those trades total at least (US$, optional)
        <input
          type="number"
          name="form4_min_value"
          min={1}
          step={1}
          inputMode="numeric"
          defaultValue={minValue ?? ''}
        />
      </label>
      <span className="muted">
        Most Form 4s are routine: shares withheld for taxes, stock awards,
        gifts. A minimum counts open-market trades only; a trade priced only in
        the filing's footnotes counts as meeting it.
      </span>
    </>
  )
}
