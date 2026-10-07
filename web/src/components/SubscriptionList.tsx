import { type FormEvent, useState } from 'react'
import { Link } from 'react-router'
import { useDeleteSubscription, useUpdateSubscription } from '../api/account'
import type { Subscription } from '../api/client'
import { formatDateTime, formLabel } from '../lib/format'
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

function formsLabel(forms: string[]) {
  return forms.length === 0 ? 'all forms' : forms.map(formLabel).join(' and ')
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
                  <Destination
                    key={subscription.id}
                    subscription={subscription}
                    onRemove={() => remove.mutate(subscription.id)}
                    removing={remove.isPending}
                  />
                ))}
              </ul>
            </li>
          )
        })}
      </ul>
    </>
  )
}

type DestinationProps = {
  subscription: Subscription
  onRemove: () => void
  removing: boolean
}

function Destination({ subscription, onRemove, removing }: DestinationProps) {
  const [editing, setEditing] = useState(false)
  const update = useUpdateSubscription()
  const channel = CHANNEL_NAMES[subscription.channel]
  const label = `${subscription.company.ticker} ${channel} alert`

  function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const forms = new FormData(event.currentTarget).getAll('form') as (
      '4' | '8-K'
    )[]
    update.mutate(
      { id: subscription.id, form_types: forms },
      { onSuccess: () => setEditing(false) },
    )
  }

  return (
    <li className={styles.destination}>
      <div className={styles.row}>
        <span>
          {channel} <span className={styles.target}>{subscription.target}</span>{' '}
          <span className="muted">({formsLabel(subscription.form_types)})</span>
        </span>
        <span className={styles.actions}>
          <button
            type="button"
            onClick={() => setEditing(!editing)}
            aria-expanded={editing}
            aria-label={`Edit the forms for the ${label}`}
          >
            Edit forms
          </button>
          <button
            type="button"
            onClick={onRemove}
            disabled={removing}
            aria-label={`Remove the ${label}`}
          >
            Remove
          </button>
        </span>
      </div>
      <LastDelivery subscription={subscription} />
      {editing && (
        <form
          className={styles.edit}
          onSubmit={save}
          aria-label={`Forms for the ${label}`}
        >
          <label className={styles.inline}>
            <input
              type="checkbox"
              name="form"
              value="4"
              defaultChecked={subscription.form_types.includes('4')}
            />{' '}
            Form 4
          </label>
          <label className={styles.inline}>
            <input
              type="checkbox"
              name="form"
              value="8-K"
              defaultChecked={subscription.form_types.includes('8-K')}
            />{' '}
            8-K
          </label>
          <span className="muted">None checked means all forms.</span>
          <button type="submit" disabled={update.isPending}>
            {update.isPending ? 'Saving…' : 'Save'}
          </button>
          {update.isError && <ErrorMessage error={update.error} />}
        </form>
      )}
    </li>
  )
}

function LastDelivery({ subscription }: { subscription: Subscription }) {
  const last = subscription.last_delivery
  if (!last) return <p className={styles.status}>No alerts yet.</p>
  const when = formatDateTime(last.at)
  const filing = <Link to={`/filings/${last.accession_no}`}>{when}</Link>
  if (last.status === 'sent') {
    return <p className={styles.status}>Last alert sent {filing}.</p>
  }
  if (last.status === 'pending') {
    return (
      <p className={styles.status}>
        Last alert ({filing}) hasn't gone through yet; it will be retried.
      </p>
    )
  }
  return (
    <p className={`${styles.status} ${styles.failed}`} role="status">
      Last alert failed ({filing}): {last.error ?? 'rejected'}.{' '}
      {subscription.channel === 'email'
        ? 'Check your email address.'
        : 'If the webhook was deleted, remove this alert and add it again with a new one.'}
    </p>
  )
}
