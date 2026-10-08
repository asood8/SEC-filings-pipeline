import { Fragment } from 'react'
import type { ReportingOwner } from '../api/client'

/**
 * A Form 4's reporting owners by their readable names ("Timothy D Cook"), with the name as filed
 * ("COOK TIMOTHY D") on hover: names are reordered by rule, which an unusual name can defeat.
 */
export function OwnerNames({ owners }: { owners: ReportingOwner[] }) {
  return (
    <>
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
        </Fragment>
      ))}
    </>
  )
}
