import { useEffect } from 'react'
import { SITE_NAME } from './site'

/** "Apple Inc. (AAPL) · SEC Filings", or the site's name alone. */
export function pageTitle(title?: string): string {
  return title ? `${title} · ${SITE_NAME}` : SITE_NAME
}

/**
 * Names the current page in the browser tab, history, and bookmarks, and for screen readers.
 * Pass undefined while what the page is about is still loading: the tab shows the site's name.
 */
export function usePageTitle(title?: string): void {
  useEffect(() => {
    document.title = pageTitle(title)
  }, [title])
}
