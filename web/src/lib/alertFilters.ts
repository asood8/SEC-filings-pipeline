import type { Subscription } from '../api/client'
import { formatMoney } from './format'
import { ITEMS_8K } from './glossary'

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

/** The 8-K items offered as alert filters: the ones that say what happened. 9.01 (exhibits)
 * comes with most 8-Ks, so it is left out; the API accepts any item. */
export const ALERT_8K_ITEMS = [
  '1.01', '1.05', '2.01', '2.02', '2.03', '2.05', '2.06', '3.01',
  '4.01', '4.02', '5.01', '5.02', '5.07', '7.01', '8.01',
].map((item) => ({ item, label: ITEMS_8K[item] ?? item })) // prettier-ignore

export function readForm8KItems(data: FormData): string[] {
  return data.getAll('form8k_item').map(String)
}

/** "8-K: 2.02, 5.02", or null for every 8-K. */
export function form8kItemsLabel(subscription: Subscription): string | null {
  const items = subscription.form8k_items
  return items.length ? `8-K: ${items.join(', ')}` : null
}
