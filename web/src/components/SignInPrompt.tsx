import type { ReactNode } from 'react'
import { useSignIn } from '../api/account'
import { GoogleSignInButton } from './GoogleSignInButton'
import { ErrorMessage, Loading } from './QueryStatus'

/** Google's sign-in button, after a line saying what signing in is for. */
export function SignInPrompt({ children }: { children: ReactNode }) {
  const signIn = useSignIn()
  return (
    <section>
      <p>{children}</p>
      <GoogleSignInButton
        onCredential={(credential) => signIn.mutate(credential)}
      />
      {signIn.isPending && <Loading label="Signing in…" />}
      {signIn.isError && <ErrorMessage error={signIn.error} />}
    </section>
  )
}
