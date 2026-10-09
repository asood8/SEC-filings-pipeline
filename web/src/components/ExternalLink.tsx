import type { ReactNode } from 'react'

/** A link to another site, opened in a new tab: marked with an arrow, and said in words to
 * screen readers (the arrow itself is hidden from them). */
export function ExternalLink({
  href,
  children,
}: {
  href: string
  children: ReactNode
}) {
  return (
    <a href={href} target="_blank" rel="noreferrer">
      {children} <span aria-hidden="true">↗</span>
      <span className="visually-hidden">(opens in a new tab)</span>
    </a>
  )
}
