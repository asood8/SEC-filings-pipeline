import type { Subscription } from '../api/client'
import { formatMoney } from './format'

export type Form4Trades = Subscription['form4_trades']

/** The filters as the API takes them; an empty minimum is null (no minimum). */
export function readForm4Filters(data: FormData): {
  form4_trades: Form4Trades
  form4_min_value: number | null
} {
  const minimum = String(data.get('form4_min_value') ?? '').trim()
  return {
    form4_trades: (data.get('form4_trades') ?? 'all') as Form4Trades,
    form4_min_value: minimum ? Number(minimum) : null,
  }
}

/** "purchases only, $100,000 or more", or null for every Form 4. */
export function form4FilterLabel(subscription: Subscription): string | null {
  const { form4_trades: trades, form4_min_value: minimum } = subscription
  const parts = []
  if (trades === 'open_market') parts.push('open-market trades only')
  if (trades === 'purchases') parts.push('purchases only')
  if (minimum != null) parts.push(`${formatMoney(minimum)} or more`)
  return parts.length ? `Form 4: ${parts.join(', ')}` : null
}
