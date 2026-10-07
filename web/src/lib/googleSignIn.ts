/** Google Identity Services: the "Sign in with Google" button, loaded only when needed.
 * https://developers.google.com/identity/gsi/web */

type CredentialResponse = { credential: string }

export type GoogleIdentity = {
  accounts: {
    id: {
      initialize(config: {
        client_id: string
        callback: (response: CredentialResponse) => void
      }): void
      renderButton(parent: HTMLElement, options: Record<string, string>): void
      disableAutoSelect(): void
    }
  }
}

declare global {
  interface Window {
    google?: GoogleIdentity
  }
}

const SCRIPT_URL = 'https://accounts.google.com/gsi/client'

/** Public: it identifies this site to Google. The API checks tokens are issued for it. */
export const GOOGLE_CLIENT_ID: string | undefined =
  import.meta.env.GOOGLE_CLIENT_ID || undefined

let loading: Promise<GoogleIdentity> | undefined

export function loadGoogleSignIn(): Promise<GoogleIdentity> {
  loading ??= new Promise<GoogleIdentity>((resolve, reject) => {
    const script = document.createElement('script')
    script.src = SCRIPT_URL
    script.async = true
    script.onload = () =>
      window.google
        ? resolve(window.google)
        : reject(new Error('Google sign-in did not load.'))
    script.onerror = () => {
      loading = undefined // allow a retry
      reject(new Error("Couldn't load Google sign-in. Check your connection."))
    }
    document.head.append(script)
  })
  return loading
}
