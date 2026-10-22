import { useQuery } from '@tanstack/react-query'
import { type FormEvent, useState } from 'react'
import { useCreateSubscriptions } from '../api/account'
import type { Destination, Subscription, User } from '../api/client'
import { watchlistQuery } from '../api/queries'
import { readForm4Filters, readForm8KItems } from '../lib/alertFilters'
import { Form4FilterFields, Form8KItemFields } from './AlertFilters'
import { ErrorMessage } from './QueryStatus'
import styles from './Subscriptions.module.css'

type Webhook = 'discord' | 'slack'

const WEBHOOKS: { channel: Webhook; label: string; placeholder: string }[] = [
  {
    channel: 'discord',
    label: 'Discord',
    placeholder: 'https://discord.com/api/webhooks/…',
  },
  {
    channel: 'slack',
    label: 'Slack',
    placeholder: 'https://hooks.slack.com/services/…',
  },
]

const NEW_WEBHOOK = 'new'

/** One entry per webhook already in use: its masked URL and a subscription to copy it from. */
function savedWebhooks(subscriptions: Subscription[], channel: Webhook) {
  const byTarget = new Map<string, number>()
  for (const s of subscriptions) {
    if (s.channel === channel && !byTarget.has(s.target))
      byTarget.set(s.target, s.id)
  }
  return [...byTarget].map(([target, id]) => ({ target, id }))
}

type Props = {
  user: User
  subscriptions: Subscription[]
  initialTicker?: string
}

export function SubscriptionForm({
  user,
  subscriptions,
  initialTicker,
}: Props) {
  const companies = useQuery(watchlistQuery())
  const create = useCreateSubscriptions()
  const [tickers, setTickers] = useState<string[]>(
    initialTicker ? [initialTicker.toUpperCase()] : [],
  )
  const [filter, setFilter] = useState('')
  const [channels, setChannels] = useState({
    discord: false,
    slack: false,
    email: false,
  })
  const [result, setResult] = useState<string>()

  const needle = filter.trim().toLowerCase()
  const shown = (companies.data?.items ?? []).filter(
    (c) =>
      tickers.includes(c.ticker) ||
      c.ticker.toLowerCase().includes(needle) ||
      c.name.toLowerCase().includes(needle),
  )
  const destinations = Object.values(channels).filter(Boolean).length
  const total = tickers.length * destinations

  function toggleTicker(ticker: string) {
    setTickers((current) =>
      current.includes(ticker)
        ? current.filter((t) => t !== ticker)
        : [...current, ticker],
    )
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = event.currentTarget
    const data = new FormData(form)
    const chosen: Destination[] = []
    for (const { channel } of WEBHOOKS) {
      if (!channels[channel]) continue
      const saved = data.get(`${channel}-saved`)?.toString()
      chosen.push(
        saved && saved !== NEW_WEBHOOK
          ? { channel, target_from: Number(saved) }
          : { channel, target: String(data.get(`${channel}-url`)).trim() },
      )
    }
    if (channels.email) chosen.push({ channel: 'email' })
    setResult(undefined)
    create.mutate(
      {
        tickers,
        form_types: data.getAll('form') as SubscriptionBatchForms,
        ...readForm4Filters(data),
        form8k_items: readForm8KItems(data),
        destinations: chosen,
      },
      {
        onSuccess: ({ created, existing }) => {
          const added = `Added ${created.length} alert${created.length === 1 ? '' : 's'}.`
          setResult(
            existing.length
              ? `${added} ${existing.length} already existed.`
              : added,
          )
          form.reset()
          setTickers([])
          setChannels({ discord: false, slack: false, email: false })
        },
      },
    )
  }

  return (
    <form className={styles.form} onSubmit={submit}>
      <h2>Add alerts</h2>
      <fieldset>
        <legend>
          Companies <span className="muted">({tickers.length} selected)</span>
        </legend>
        <input
          type="search"
          placeholder="Filter by name or ticker"
          aria-label="Filter companies"
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
        />
        <div className={styles.companies}>
          {shown.map((company) => (
            <label key={company.cik} className={styles.chip}>
              <input
                type="checkbox"
                checked={tickers.includes(company.ticker)}
                onChange={() => toggleTicker(company.ticker)}
              />
              <span className={styles.ticker}>{company.ticker}</span>
              <span className="muted">{company.name}</span>
            </label>
          ))}
        </div>
      </fieldset>

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

      <fieldset>
        <legend>Which insider trades</legend>
        <Form4FilterFields />
      </fieldset>

      <Form8KItemFields />

      <fieldset>
        <legend>Send to (one or more)</legend>
        {WEBHOOKS.map(({ channel, label, placeholder }) => (
          <WebhookDestination
            key={channel}
            channel={channel}
            label={label}
            placeholder={placeholder}
            checked={channels[channel]}
            onToggle={(on) => setChannels((c) => ({ ...c, [channel]: on }))}
            saved={savedWebhooks(subscriptions, channel)}
          />
        ))}
        <label className={styles.inline}>
          <input
            type="checkbox"
            checked={channels.email}
            onChange={(event) =>
              setChannels((c) => ({ ...c, email: event.target.checked }))
            }
          />
          Email to {user.email}
        </label>
      </fieldset>

      {create.isError && <ErrorMessage error={create.error} />}
      {/* Always in the page, so screen readers announce the result when it appears. */}
      <div role="status">{result && <p className="muted">{result}</p>}</div>
      <p>
        <button type="submit" disabled={total === 0 || create.isPending}>
          {create.isPending
            ? 'Saving…'
            : total === 0
              ? 'Choose companies and where to send'
              : `Add ${total} alert${total === 1 ? '' : 's'}`}
        </button>
      </p>
    </form>
  )
}

type SubscriptionBatchForms = ('4' | '8-K')[]

type WebhookProps = {
  channel: Webhook
  label: string
  placeholder: string
  checked: boolean
  onToggle: (checked: boolean) => void
  saved: { target: string; id: number }[]
}

function WebhookDestination({
  channel,
  label,
  placeholder,
  checked,
  onToggle,
  saved,
}: WebhookProps) {
  const [choice, setChoice] = useState(saved[0]?.id.toString() ?? NEW_WEBHOOK)
  const pasting = saved.length === 0 || choice === NEW_WEBHOOK
  return (
    <div className={styles.destination}>
      <label className={styles.inline}>
        <input
          type="checkbox"
          checked={checked}
          onChange={(event) => onToggle(event.target.checked)}
        />
        {label}
      </label>
      {checked && (
        <div className={styles.webhook}>
          {saved.length > 0 && (
            <label>
              <span className="visually-hidden">{label} webhook</span>
              <select
                name={`${channel}-saved`}
                value={choice}
                onChange={(event) => setChoice(event.target.value)}
              >
                {saved.map(({ target, id }) => (
                  <option key={id} value={id}>
                    Same webhook as before ({target.slice(-5)})
                  </option>
                ))}
                <option value={NEW_WEBHOOK}>A new webhook URL…</option>
              </select>
            </label>
          )}
          {pasting && (
            <label>
              <span className="visually-hidden">{label} webhook URL</span>
              <input
                name={`${channel}-url`}
                type="url"
                required
                placeholder={placeholder}
                autoComplete="off"
              />
            </label>
          )}
        </div>
      )}
    </div>
  )
}
