import { Link } from 'react-router'
import { useDeleteSubscription } from '../api/account'
import type { Subscription } from '../api/client'
import { formLabel } from '../lib/format'
import { ErrorMessage } from './QueryStatus'
import styles from './Subscriptions.module.css'

const CHANNEL_NAMES = { discord: 'Discord', slack: 'Slack', email: 'Email' }

/** Subscriptions grouped by company, in the order they were first added. */
function byCompany(subscriptions: Subscription[]) {
  const groups = new Map<number, Subscription[]>()
  for (const s of subscriptions) {
    groups.set(s.company.cik, [...(groups.get(s.company.cik) ?? []), s])
  }
  return [...groups.values()]
}

export function SubscriptionList({
  subscriptions,
}: {
  subscriptions: Subscription[]
}) {
  const remove = useDeleteSubscription()

  if (subscriptions.length === 0) {
    return <p className="muted">No alerts yet. Add some below.</p>
  }
  return (
    <>
      {remove.isError && <ErrorMessage error={remove.error} />}
      <p className="muted">{subscriptions.length} of 25 alerts used.</p>
      <ul className={styles.list}>
        {byCompany(subscriptions).map((group) => {
          const { company } = group[0]
          return (
            <li key={company.cik} className={styles.item}>
              <div>
                <Link
                  to={`/companies/${company.ticker}`}
                  className={styles.ticker}
                >
                  {company.ticker}
                </Link>{' '}
                {company.name}
              </div>
              <ul className={styles.destinations}>
                {group.map((subscription) => (
                  <li key={subscription.id}>
                    <span>
                      {CHANNEL_NAMES[subscription.channel]}{' '}
                      <span className={styles.target}>
                        {subscription.target}
                      </span>{' '}
                      <span className="muted">
                        (
                        {subscription.form_types.length === 0
                          ? 'all forms'
                          : subscription.form_types
                              .map(formLabel)
                              .join(' and ')}
                        )
                      </span>
                    </span>
                    <button
                      type="button"
                      onClick={() => remove.mutate(subscription.id)}
                      disabled={remove.isPending}
                      aria-label={`Remove the ${company.ticker} ${CHANNEL_NAMES[subscription.channel]} alert`}
                    >
                      Remove
                    </button>
                  </li>
                ))}
              </ul>
            </li>
          )
        })}
      </ul>
    </>
  )
}
