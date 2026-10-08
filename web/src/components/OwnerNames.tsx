import { Fragment } from 'react'
import type { ReportingOwner } from '../api/client'
import styles from './OwnerNames.module.css'

/**
 * A Form 4's reporting owners by their readable names ("Timothy D Cook"), each followed by who
 * they are to the company ("(Executive Chair, Director)", "(10% owner)") when the API knows it.
 * Hovering shows what the label shortened: the name as filed ("COOK TIMOTHY D"), since names
 * are reordered by rule, and an officer title as filed ("Chief Financial Officer" for "CFO").
 *
 * One element, so a table cell laid out as a flex row on phones wraps it as one line of text.
 */
export function OwnerNames({ owners }: { owners: ReportingOwner[] }) {
  return (
    <span>
      {owners.map((owner, i) => (
        <Fragment key={owner.cik ?? owner.name}>
          {i > 0 && '; '}
          <span
            title={
              owner.name === owner.display_name
                ? undefined
                : `As filed: ${owner.name}`
            }
          >
            {owner.display_name}
          </span>
          {owner.relationship && (
            <>
              {' '}
              <span className={styles.relationship} title={filedTitle(owner)}>
                ({owner.relationship})
              </span>
            </>
          )}
        </Fragment>
      ))}
    </span>
  )
}

/** The officer title as filed, when the label shows it shortened or tidied. */
function filedTitle(owner: ReportingOwner): string | undefined {
  const filed = owner.officer_title
  return filed && !owner.relationship?.includes(filed)
    ? `Title as filed: ${filed}`
    : undefined
}
