import { useEffect, useRef, useState } from 'react'
import { GOOGLE_CLIENT_ID, loadGoogleSignIn } from '../lib/googleSignIn'

type Props = { onCredential: (credential: string) => void }

/** Google's own button: it opens Google's sign-in and hands back an ID token, which the API
 * exchanges for a session cookie. */
export function GoogleSignInButton({ onCredential }: Props) {
  const container = useRef<HTMLDivElement>(null)
  const callback = useRef(onCredential)
  const [error, setError] = useState<string>()

  useEffect(() => {
    callback.current = onCredential
  })

  useEffect(() => {
    const clientId = GOOGLE_CLIENT_ID
    if (!clientId) return
    let cancelled = false
    loadGoogleSignIn().then(
      (google) => {
        if (cancelled || !container.current) return
        google.accounts.id.initialize({
          client_id: clientId,
          callback: (response) => callback.current(response.credential),
        })
        google.accounts.id.renderButton(container.current, {
          type: 'standard',
          theme: 'outline',
          size: 'large',
          text: 'signin_with',
        })
      },
      (reason: Error) => !cancelled && setError(reason.message),
    )
    return () => {
      cancelled = true
    }
  }, [])

  if (!GOOGLE_CLIENT_ID) {
    return (
      <p className="notice">
        Sign-in isn't configured: set GOOGLE_CLIENT_ID in the repository's .env.
      </p>
    )
  }
  return (
    <>
      <div ref={container} />
      {error && (
        <p className="notice" role="alert">
          {error}
        </p>
      )}
    </>
  )
}
