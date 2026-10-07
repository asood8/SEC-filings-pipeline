import { useQuery } from '@tanstack/react-query'
import { type FormEvent, useState } from 'react'
import { useCreateSubscription } from '../api/account'
import type { Channel, NewSubscription, User } from '../api/client'
import { watchlistQuery } from '../api/queries'
import { ErrorMessage } from './QueryStatus'
import styles from './Subscriptions.module.css'

const CHANNELS: { value: Channel; label: string; placeholder?: string }[] = [
  {
    value: 'discord',
    label: 'Discord',
    placeholder: 'https://discord.com/api/webhooks/…',
  },
  {
    value: 'slack',
    label: 'Slack',
    placeholder: 'https://hooks.slack.com/services/…',
  },
  { value: 'email', label: 'Email' },
]

type Props = { user: User; initialTicker?: string }

export function SubscriptionForm({ user, initialTicker = '' }: Props) {
  const companies = useQuery(watchlistQuery())
  const create = useCreateSubscription()
  // Controlled: the companies load after the form renders, so a default value would be lost.
  const [ticker, setTicker] = useState(initialTicker.toUpperCase())
  const [channel, setChannel] = useState<Channel>('discord')
  const webhook = CHANNELS.find((c) => c.value === channel)?.placeholder

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = event.currentTarget
    const data = new FormData(form)
    const body: NewSubscription = {
      ticker,
      channel,
      form_types: data.getAll('form') as NewSubscription['form_types'],
      target: webhook ? String(data.get('target')).trim() : null,
    }
    create.mutate(body, {
      onSuccess: () => {
        form.reset()
        setTicker('')
      },
    })
  }

  return (
    <form className={styles.form} onSubmit={submit}>
      <h2>Add an alert</h2>
      <label>
        Company
        <select
          name="ticker"
          value={ticker}
          onChange={(event) => setTicker(event.target.value)}
          required
        >
          <option value="" disabled>
            Choose a company
          </option>
          {companies.data?.items.map((company) => (
            <option key={company.cik} value={company.ticker}>
              {company.ticker}: {company.name}
            </option>
          ))}
        </select>
      </label>
      <fieldset>
        <legend>Forms (none checked means all)</legend>
        <label className={styles.inline}>
          <input type="checkbox" name="form" value="4" /> Insider trades (Form
          4)
        </label>
        <label className={styles.inline}>
          <input type="checkbox" name="form" value="8-K" /> Current reports
          (8-K)
        </label>
      </fieldset>
      <label>
        Send to
        <select
          name="channel"
          value={channel}
          onChange={(event) => setChannel(event.target.value as Channel)}
        >
          {CHANNELS.map((c) => (
            <option key={c.value} value={c.value}>
              {c.label}
            </option>
          ))}
        </select>
      </label>
      {webhook ? (
        <label>
          Webhook URL
          <input
            name="target"
            type="url"
            required
            placeholder={webhook}
            autoComplete="off"
          />
          <span className="muted">
            Anyone with this URL can post to the channel, so keep it private.
            After saving, only its last 4 characters are shown.
          </span>
        </label>
      ) : (
        <p className="muted">
          Email alerts go to your account's address, {user.email}.
        </p>
      )}
      {create.isError && <ErrorMessage error={create.error} />}
      <p>
        <button type="submit" disabled={create.isPending}>
          {create.isPending ? 'Saving…' : 'Add alert'}
        </button>
      </p>
    </form>
  )
}
