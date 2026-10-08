import { type FormEvent } from 'react'
import { Link, NavLink, Outlet, useNavigate } from 'react-router'
import { API_BASE_URL } from '../api/client'
import styles from './Layout.module.css'

export function Layout() {
  const navigate = useNavigate()

  function search(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const q = new FormData(event.currentTarget).get('q')?.toString().trim()
    if (q) navigate(`/search?${new URLSearchParams({ q })}`)
  }

  return (
    <>
      <header className={styles.header}>
        <div className={styles.bar}>
          <Link to="/" className={styles.brand}>
            SEC Filings
          </Link>
          <nav className={styles.nav} aria-label="Main">
            <NavLink to="/" end>
              Latest
            </NavLink>
            <NavLink to="/insiders">Insider trades</NavLink>
            <NavLink to="/companies">Companies</NavLink>
            <NavLink to="/search">Search</NavLink>
            <NavLink to="/alerts">Alerts</NavLink>
          </nav>
          <form className={styles.search} role="search" onSubmit={search}>
            <label htmlFor="header-search" className="visually-hidden">
              Search filings
            </label>
            <input
              id="header-search"
              name="q"
              type="search"
              placeholder="Search filings…"
            />
          </form>
        </div>
      </header>
      <main className={styles.main}>
        <Outlet />
      </main>
      <footer className={styles.footer}>
        <p>
          Summaries are generated automatically and may contain errors. They are
          not investment advice; every filing links to the original on sec.gov.
          Data from SEC EDGAR.
        </p>
        <p>
          <a href={`${API_BASE_URL}/docs`}>API documentation</a>
        </p>
      </footer>
    </>
  )
}
