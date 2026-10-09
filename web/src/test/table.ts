import { within } from '@testing-library/react'

/** A table row's text, cell by cell: its row header (the row's title) first, then its cells. */
export function rowText(row: HTMLElement): (string | null)[] {
  return [
    ...within(row).queryAllByRole('rowheader'),
    ...within(row).getAllByRole('cell'),
  ].map((cell) => cell.textContent)
}
