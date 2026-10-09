import {
  type FormEvent,
  type MouseEvent,
  type RefObject,
  useEffect,
  useRef,
} from 'react'
import { Link, NavLink, Outlet, useLocation, useNavigate } from 'react-router'
import { API_BASE_URL } from '../api/client'
import { SITE_NAME } from '../lib/site'
import styles from './Layout.module.css'

export function Layout() {
  const navigate = useNavigate()
  const main = useRef<HTMLElement>(null)
  useFocusOnNavigation(main)

  function search(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const q = new FormData(event.currentTarget).get('q')?.toString().trim()
    if (q) navigate(`/search?${new URLSearchParams({ q })}`)
  }

  // Focus moves without changing the URL: a #main hash would be a navigation of its own.
  function skipToMain(event: MouseEvent<HTMLAnchorElement>) {
    event.preventDefault()
    main.current?.focus()
  }

  return (
    <>
      <a href="#main" className="skip-link" onClick={skipToMain}>
        Skip to main content
      </a>
      <header className={styles.header}>
        <div className={styles.bar}>
          <Link to="/" className={styles.brand}>
            {SITE_NAME}
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
      <main id="main" ref={main} tabIndex={-1} className={styles.main}>
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

/**
 * After moving to another page, puts keyboard and screen reader focus on the new page's heading
 * (or on <main> while the page is still loading), as a full page load would start reading
 * there. Without it, focus stays on the link just followed, and a screen reader says nothing
 * about the new page. Changes to the query string alone (filters, a new search) leave focus
 * where it is, and so does the first load, so the skip link stays the first stop.
 */
function useFocusOnNavigation(main: RefObject<HTMLElement | null>) {
  const { pathname } = useLocation()
  const previous = useRef(pathname)
  useEffect(() => {
    if (previous.current === pathname) return
    previous.current = pathname
    const heading = main.current?.querySelector('h1')
    const target = heading ?? main.current
    if (!target) return
    if (heading && !heading.hasAttribute('tabindex'))
      heading.setAttribute('tabindex', '-1')
    target.focus({ preventScroll: true })
  }, [main, pathname])
}
