import { useQuery } from '@tanstack/react-query'
import { type FormEvent } from 'react'
import { useSearchParams } from 'react-router'
import { searchQuery, type SearchMode } from '../api/queries'
import { FilingList } from '../components/FilingList'
import { ErrorMessage, Loading } from '../components/QueryStatus'
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

export function SearchPage() {
  const [params, setParams] = useSearchParams()
  const q = params.get('q') ?? ''
  const mode = parseMode(params.get('mode'))
  const formType = params.get('form') ?? ''
  const query = useQuery(
    searchQuery({ q, mode, formType: formType || undefined }),
  )

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    const next = new URLSearchParams()
    for (const key of ['q', 'mode', 'form']) {
      const value = form.get(key)?.toString().trim()
      if (value) next.set(key, value)
    }
    setParams(next)
  }

  return (
    <>
      <title>
        {q ? `${q} · Search · SEC Filings` : 'Search · SEC Filings'}
      </title>
      <h1>Search filings</h1>
      {/* Keyed by the URL so the fields reset when the header search changes it. */}
      <form key={params.toString()} className={styles.form} onSubmit={submit}>
        <label htmlFor="search-q" className="visually-hidden">
          Search for
        </label>
        <input
          id="search-q"
          name="q"
          type="search"
          defaultValue={q}
          placeholder="e.g. CEO resignation, stock buyback, Tim Cook"
          maxLength={200}
        />
        <label>
          <span className="visually-hidden">Match</span>
          <select name="mode" defaultValue={mode}>
            {MODES.map((m) => (
              <option key={m.value} value={m.value}>
                {m.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span className="visually-hidden">Form type</span>
          <select name="form" defaultValue={formType}>
            <option value="">All forms</option>
            <option value="4">Form 4</option>
            <option value="8-K">8-K</option>
          </select>
        </label>
        <button type="submit">Search</button>
      </form>
      <p className="muted">
        “Exact words” understands <code>"quoted phrases"</code>, <code>OR</code>
        , and <code>-excluded</code> words. “Similar meaning” finds related
        wording, so “leadership change” finds CEO appointments.
      </p>

      {!q ? null : query.isPending ? (
        <Loading label="Searching…" />
      ) : query.isError ? (
        <ErrorMessage error={query.error} />
      ) : (
        <section aria-label="Results">
          {query.data.degraded && (
            <p className="notice">
              Meaning-based search is unavailable right now, so these results
              match exact words only.
            </p>
          )}
          {query.data.items.length === 0 ? (
            <p>No filings match “{q}”.</p>
          ) : (
            <FilingList filings={query.data.items} />
          )}
        </section>
      )}
    </>
  )
}
