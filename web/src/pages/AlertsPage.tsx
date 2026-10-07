import { useQuery } from '@tanstack/react-query'
import { useSearchParams } from 'react-router'
import {
  meQuery,
  subscriptionsQuery,
  useDeleteAccount,
  useSignIn,
  useSignOut,
} from '../api/account'
import type { User } from '../api/client'
import { GoogleSignInButton } from '../components/GoogleSignInButton'
import { ErrorMessage, Loading } from '../components/QueryStatus'
import { SubscriptionForm } from '../components/SubscriptionForm'
import { SubscriptionList } from '../components/SubscriptionList'
import { loadGoogleSignIn } from '../lib/googleSignIn'

export function AlertsPage() {
  const me = useQuery(meQuery())

  return (
    <>
      <title>Alerts · SEC Filings</title>
      <h1>Alerts</h1>
      <p className="muted">
        Get a Discord, Slack, or email message when a company you follow files a
        Form 4 or an 8-K, with its plain-English summary. One message per
        filing, never repeats.
      </p>
      {me.isPending ? (
        <Loading />
      ) : me.isError ? (
        <ErrorMessage error={me.error} />
      ) : me.data ? (
        <SignedIn user={me.data} />
      ) : (
        <SignedOut />
      )}
    </>
  )
}

function SignedOut() {
  const signIn = useSignIn()
  return (
    <section>
      <p>Sign in with your Google account to set up alerts.</p>
      <GoogleSignInButton
        onCredential={(credential) => signIn.mutate(credential)}
      />
      {signIn.isPending && <Loading label="Signing in…" />}
      {signIn.isError && <ErrorMessage error={signIn.error} />}
    </section>
  )
}

function SignedIn({ user }: { user: User }) {
  const [params] = useSearchParams()
  const subscriptions = useQuery(subscriptionsQuery())
  const signOut = useSignOut()
  const deleteAccount = useDeleteAccount()

  function leave() {
    // Stop Google from signing this browser straight back in.
    loadGoogleSignIn().then(
      (google) => google.accounts.id.disableAutoSelect(),
      () => {},
    )
    signOut.mutate()
  }

  return (
    <>
      <p>
        Signed in as <strong>{user.email}</strong>.{' '}
        <button type="button" onClick={leave} disabled={signOut.isPending}>
          Sign out
        </button>
      </p>
      {signOut.isError && <ErrorMessage error={signOut.error} />}

      <h2>Your alerts</h2>
      {subscriptions.isPending ? (
        <Loading />
      ) : subscriptions.isError ? (
        <ErrorMessage error={subscriptions.error} />
      ) : (
        <SubscriptionList subscriptions={subscriptions.data} />
      )}
      <SubscriptionForm
        user={user}
        initialTicker={params.get('ticker') ?? ''}
      />

      <details>
        <summary>Delete your account</summary>
        <p>This removes your account, every alert, and your alert history.</p>
        <button
          type="button"
          onClick={() => deleteAccount.mutate()}
          disabled={deleteAccount.isPending}
        >
          Delete my account
        </button>
        {deleteAccount.isError && <ErrorMessage error={deleteAccount.error} />}
      </details>
    </>
  )
}
