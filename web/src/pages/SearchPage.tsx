import { useQuery } from '@tanstack/react-query'
import { type FormEvent, useState } from 'react'
import { useSearchParams } from 'react-router'
import { SEARCH_LIMIT, searchQuery, type SearchMode } from '../api/queries'
import { FilingList } from '../components/FilingList'
import { ErrorMessage } from '../components/QueryStatus'
import styles from './SearchPage.module.css'

const MODES: { value: SearchMode; label: string }[] = [
  { value: 'hybrid', label: 'Best match' },
  { value: 'keyword', label: 'Exact words' },
  { value: 'semantic', label: 'Similar meaning' },
]

function parseMode(value: string | null): SearchMode {
  return MODES.some((mode) => mode.value === value)
    ? (value as SearchMode)
    : 'hybrid'
}

type Draft = { q: string; mode: SearchMode; form: string }

export function SearchPage() {
  const [params, setParams] = useSearchParams()
  const q = params.get('q') ?? ''
  const mode = parseMode(params.get('mode'))
  const formType = params.get('form') ?? ''
  const query = useQuery(
    searchQuery({ q, mode, formType: formType || undefined }),
  )

  // The fields follow the URL (the header's search box changes it too). They're updated in
  // place rather than re-mounted, so focus stays where it was after a search.
  const fromUrl: Draft = { q, mode, form: formType }
  const [draft, setDraft] = useState(fromUrl)
  const [draftFor, setDraftFor] = useState(params.toString())
  if (draftFor !== params.toString()) {
    setDraftFor(params.toString())
    setDraft(fromUrl)
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const next = new URLSearchParams()
    const fields = { q: draft.q.trim(), mode: draft.mode, form: draft.form }
    for (const [key, value] of Object.entries(fields)) {
      if (value) next.set(key, value)
    }
    setParams(next)
  }

  const count = query.data?.items.length ?? 0
  // Background refreshes of the same search stay silent.
  const status = !q
    ? ''
    : query.isPending || query.isPlaceholderData
      ? 'Searching…'
      : !query.data
        ? ''
        : count === 0
          ? `No filings match “${q}”.`
          : count >= SEARCH_LIMIT
            ? `The ${count} best matches for “${q}”.`
            : `${count} ${count === 1 ? 'match' : 'matches'} for “${q}”.`

  return (
    <>
      <title>
        {q ? `${q} · Search · SEC Filings` : 'Search · SEC Filings'}
      </title>
      <h1>Search filings</h1>
      <form className={styles.form} onSubmit={submit}>
        <label htmlFor="search-q" className="visually-hidden">
          Search for
        </label>
        <input
          id="search-q"
          name="q"
          type="search"
          value={draft.q}
          onChange={(event) => setDraft({ ...draft, q: event.target.value })}
          placeholder="e.g. CEO resignation, stock buyback, Tim Cook"
          maxLength={200}
          aria-describedby="search-help"
        />
        <label className={styles.field}>
          Match
          <select
            name="mode"
            value={draft.mode}
            onChange={(event) =>
              setDraft({ ...draft, mode: parseMode(event.target.value) })
            }
          >
            {MODES.map((m) => (
              <option key={m.value} value={m.value}>
                {m.label}
              </option>
            ))}
          </select>
        </label>
        <label className={styles.field}>
          Form type
          <select
            name="form"
            value={draft.form}
            onChange={(event) =>
              setDraft({ ...draft, form: event.target.value })
            }
          >
            <option value="">All forms</option>
            <option value="4">Form 4</option>
            <option value="8-K">8-K</option>
          </select>
        </label>
        <button type="submit">Search</button>
      </form>
      <p id="search-help" className="muted">
        “Exact words” understands <code>"quoted phrases"</code>, <code>OR</code>
        , and <code>-excluded</code> words. “Similar meaning” finds related
        wording, so “leadership change” finds CEO appointments.
      </p>

      {/* Always in the page, so screen readers announce each change: searching, then how many
          filings matched. */}
      <div role="status" className={styles.status}>
        {status && <p>{status}</p>}
      </div>
      {q && query.isError ? (
        <ErrorMessage error={query.error} />
      ) : q && query.data ? (
        <section aria-label="Results">
          {query.data.degraded && (
            <p className="notice">
              Meaning-based search is unavailable right now, so these results
              match exact words only.
            </p>
          )}
          <FilingList filings={query.data.items} />
        </section>
      ) : null}
    </>
  )
}
