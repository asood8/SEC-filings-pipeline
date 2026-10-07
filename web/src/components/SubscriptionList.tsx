import { Link } from 'react-router'
import { useDeleteSubscription } from '../api/account'
import type { Subscription } from '../api/client'
import { formLabel } from '../lib/format'
import { ErrorMessage } from './QueryStatus'
import styles from './Subscriptions.module.css'

const CHANNEL_NAMES = { discord: 'Discord', slack: 'Slack', email: 'Email' }

export function SubscriptionList({
  subscriptions,
}: {
  subscriptions: Subscription[]
}) {
  const remove = useDeleteSubscription()

  if (subscriptions.length === 0) {
    return <p className="muted">No alerts yet. Add one below.</p>
  }
  return (
    <>
      {remove.isError && <ErrorMessage error={remove.error} />}
      <ul className={styles.list}>
        {subscriptions.map((subscription) => (
          <li key={subscription.id} className={styles.item}>
            <div>
              <Link
                to={`/companies/${subscription.company.ticker}`}
                className={styles.ticker}
              >
                {subscription.company.ticker}
              </Link>{' '}
              {subscription.company.name}
              <div className="muted">
                {subscription.form_types.length === 0
                  ? 'All forms'
                  : subscription.form_types.map(formLabel).join(' and ')}{' '}
                · {CHANNEL_NAMES[subscription.channel]}:{' '}
                <span className={styles.target}>{subscription.target}</span>
              </div>
            </div>
            <button
              type="button"
              onClick={() => remove.mutate(subscription.id)}
              disabled={remove.isPending}
              aria-label={`Remove the ${subscription.company.ticker} alert`}
            >
              Remove
            </button>
          </li>
        ))}
      </ul>
    </>
  )
}
