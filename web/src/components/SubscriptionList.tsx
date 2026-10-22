import { type FormEvent, type KeyboardEvent, useRef, useState } from 'react'
import { Link } from 'react-router'
import { useDeleteSubscription, useUpdateSubscription } from '../api/account'
import type { Subscription } from '../api/client'
import { formatDateTime, formLabel } from '../lib/format'
import { ErrorMessage } from './QueryStatus'
import styles from './Subscriptions.module.css'
import { form4FilterLabel, readForm4Filters } from '../lib/form4Filters'
import { Form4FilterFields } from './Form4Filters'

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

type Props = {
  subscriptions: Subscription[]
  /** Called once an alert is gone, to put focus somewhere sensible: its button went with it. */
  onRemoved?: () => void
}

export function SubscriptionList({ subscriptions, onRemoved }: Props) {
  const remove = useDeleteSubscription()
  const [removed, setRemoved] = useState<string>()

  function removeOne(id: number, label: string) {
    if (remove.isPending) return
    setRemoved(undefined)
    remove.mutate(id, {
      onSuccess: () => {
        setRemoved(`Removed the ${label}.`)
        onRemoved?.()
      },
    })
  }

  return (
    <>
      {/* Always in the page, so screen readers announce a removal. */}
      <div role="status">{removed && <p className="muted">{removed}</p>}</div>
      {remove.isError && <ErrorMessage error={remove.error} />}
      {subscriptions.length === 0 ? (
        <p className="muted">No alerts yet. Add some below.</p>
      ) : (
        <Groups
          subscriptions={subscriptions}
          onRemove={removeOne}
          removing={remove.isPending}
        />
      )}
    </>
  )
}

function Groups({
  subscriptions,
  onRemove,
  removing,
}: {
  subscriptions: Subscription[]
  onRemove: (id: number, label: string) => void
  removing: boolean
}) {
  return (
    <>
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
                    onRemove={(label) => onRemove(subscription.id, label)}
                    removing={removing}
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
  onRemove: (label: string) => void
  removing: boolean
}

function Destination({ subscription, onRemove, removing }: DestinationProps) {
  const [editing, setEditing] = useState(false)
  const editButton = useRef<HTMLButtonElement>(null)
  const update = useUpdateSubscription()
  const channel = CHANNEL_NAMES[subscription.channel]
  const label = `${subscription.company.ticker} ${channel} alert`
  const filters = form4FilterLabel(subscription)

  /** Closes the edit form, putting focus back on the button that opened it. */
  function close() {
    setEditing(false)
    editButton.current?.focus()
  }

  function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const data = new FormData(event.currentTarget)
    const forms = data.getAll('form') as ('4' | '8-K')[]
    update.mutate(
      { id: subscription.id, form_types: forms, ...readForm4Filters(data) },
      { onSuccess: close },
    )
  }

  function closeOnEscape(event: KeyboardEvent<HTMLFormElement>) {
    if (event.key !== 'Escape') return
    event.preventDefault()
    close()
  }

  return (
    <li className={styles.destination}>
      <div className={styles.row}>
        <span>
          {channel} <span className={styles.target}>{subscription.target}</span>{' '}
          <span className="muted">({formsLabel(subscription.form_types)})</span>
          {filters && <span className="muted"> · {filters}</span>}
        </span>
        <span className={styles.actions}>
          <button
            ref={editButton}
            type="button"
            onClick={() => setEditing(!editing)}
            aria-expanded={editing}
            aria-label={`Edit the forms and filters for the ${label}`}
          >
            Edit
          </button>
          <button
            type="button"
            onClick={() => onRemove(label)}
            aria-disabled={removing || undefined}
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
          onKeyDown={closeOnEscape}
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
          <Form4FilterFields
            trades={subscription.form4_trades}
            minValue={subscription.form4_min_value}
          />
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
  const filing = (
    <Link to={`/filings/${last.accession_no}`}>
      {when}{' '}
      <span className="visually-hidden">
        (the {subscription.company.ticker} filing alerted)
      </span>
    </Link>
  )
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
