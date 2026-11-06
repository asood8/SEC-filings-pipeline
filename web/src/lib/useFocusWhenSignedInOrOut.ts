import { type RefObject, useEffect, useRef } from 'react'
import type { User } from '../api/client'

/**
 * Signing in or out replaces most of the page, including the button that was used, so focus
 * moves to the page's heading instead of being lost.
 */
export function useFocusWhenSignedInOrOut(
  user: User | null | undefined,
  heading: RefObject<HTMLHeadingElement | null>,
) {
  const signedIn = user === undefined ? undefined : user !== null
  const previous = useRef(signedIn)
  useEffect(() => {
    if (signedIn === undefined) return
    if (previous.current !== undefined && previous.current !== signedIn)
      heading.current?.focus()
    previous.current = signedIn
  }, [signedIn, heading])
}
