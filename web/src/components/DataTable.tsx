import { type ReactNode, useId } from 'react'
import styles from './DataTable.module.css'

/**
 * A table of figures with a caption for screen readers. On narrow screens the stylesheet turns
 * each row into a card, so the rows and cells inside carry explicit ARIA roles (row,
 * columnheader, rowheader, cell): some browsers stop exposing a table as a table once its CSS
 * display changes, and the roles keep each value announced with its column's header.
 *
 * The wrapper scrolls sideways when a wide table doesn't fit, so it's a named, focusable region
 * that keyboard users can scroll with the arrow keys.
 */
export function DataTable({
  caption,
  children,
}: {
  caption: string
  children: ReactNode
}) {
  const id = useId()
  return (
    <div
      className={styles.scroll}
      role="region"
      aria-labelledby={id}
      tabIndex={0}
    >
      <table className={styles.table} role="table">
        <caption id={id} className="visually-hidden">
          {caption}
        </caption>
        {children}
      </table>
    </div>
  )
}
