import {
  queryOptions,
  useMutation,
  useQueryClient,
} from '@tanstack/react-query'
import {
  api,
  ApiError,
  unwrap,
  type NewCompanyRequest,
  type RequestStatus,
  type SubscriptionBatch,
  type SubscriptionUpdate,
} from './client'
import type { paths } from './schema'

export type AlertPreviewParams = NonNullable<
  paths['/subscriptions/preview']['get']['parameters']['query']
>

/** The signed-in user, or null when signed out (the API answers 401). */
export const meQuery = () =>
  queryOptions({
    queryKey: ['me'],
    queryFn: async () => {
      try {
        return await unwrap(api.GET('/me'))
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) return null
        throw error
      }
    },
  })

export const subscriptionsQuery = () =>
  queryOptions({
    queryKey: ['subscriptions'],
    queryFn: () => unwrap(api.GET('/subscriptions')),
  })

/** Which recent filings an alert with these settings would have sent. */
export const alertPreviewQuery = (params: AlertPreviewParams) =>
  queryOptions({
    queryKey: ['alert-preview', params],
    queryFn: () =>
      unwrap(api.GET('/subscriptions/preview', { params: { query: params } })),
  })

/** Queries whose answer depends on who is signed in. */
const PER_USER = [
  ['subscriptions'],
  ['company-requests'],
  ['company-suggestions'],
]

export function useSignIn() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (credential: string) =>
      unwrap(api.POST('/auth/google', { body: { credential } })),
    onSuccess: (user) => {
      queryClient.setQueryData(meQuery().queryKey, user)
      return Promise.all(
        PER_USER.map((queryKey) => queryClient.invalidateQueries({ queryKey })),
      )
    },
  })
}

function useSignedOut() {
  const queryClient = useQueryClient()
  return () => {
    queryClient.setQueryData(meQuery().queryKey, null)
    for (const queryKey of PER_USER) queryClient.removeQueries({ queryKey })
  }
}

/** Responses without a body (204) resolve to `{ data: undefined }`: success, nothing to return. */
async function noContent(
  request: Promise<{ error?: unknown; response: Response }>,
): Promise<void> {
  const { error, response } = await request
  if (error !== undefined || !response.ok) {
    await unwrap(Promise.resolve({ error: error ?? {}, response }))
  }
}

export function useSignOut() {
  const signedOut = useSignedOut()
  return useMutation({
    mutationFn: () => noContent(api.POST('/auth/logout')),
    onSuccess: signedOut,
  })
}

export function useDeleteAccount() {
  const signedOut = useSignedOut()
  return useMutation({
    mutationFn: () => noContent(api.DELETE('/me')),
    onSuccess: signedOut,
  })
}

/** Every combination of companies and destinations, in one all-or-nothing request. */
export function useCreateSubscriptions() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: SubscriptionBatch) =>
      unwrap(api.POST('/subscriptions/batch', { body })),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ['subscriptions'] }),
  })
}

export function useUpdateSubscription() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ id, ...body }: { id: number } & SubscriptionUpdate) =>
      unwrap(
        api.PATCH('/subscriptions/{subscription_id}', {
          params: { path: { subscription_id: id } },
          body,
        }),
      ),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ['subscriptions'] }),
  })
}

export function useDeleteSubscription() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: number) =>
      noContent(
        api.DELETE('/subscriptions/{subscription_id}', {
          params: { path: { subscription_id: id } },
        }),
      ),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ['subscriptions'] }),
  })
}

/** The email alert an unsubscribe link (from an alert email) would stop. */
export const unsubscribeQuery = (token: string) =>
  queryOptions({
    queryKey: ['unsubscribe', token],
    queryFn: () =>
      unwrap(api.GET('/unsubscribe', { params: { query: { token } } })),
    retry: false,
  })

/** Stops the email alert the link names; needs no sign-in (the link is signed). */
export function useUnsubscribe(token: string) {
  return useMutation({
    mutationFn: () =>
      unwrap(api.POST('/unsubscribe', { params: { query: { token } } })),
  })
}

/** The signed-in user's suggestions of companies to follow, newest first. */
export const myCompanyRequestsQuery = () =>
  queryOptions({
    queryKey: ['company-requests'],
    queryFn: () => unwrap(api.GET('/company-requests')),
  })

export function useSuggestCompany() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: NewCompanyRequest) =>
      unwrap(api.POST('/company-requests', { body })),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ['company-requests'] }),
  })
}

/** Everyone's suggestions with this status, by company, most asked-for first (admins only). */
export const companySuggestionsQuery = (status: RequestStatus) =>
  queryOptions({
    queryKey: ['company-suggestions', status],
    queryFn: () =>
      unwrap(
        api.GET('/admin/company-requests', { params: { query: { status } } }),
      ),
  })

/** Marks a suggested company added, declined, or open again, for everyone who suggested it. */
export function useDecideSuggestion() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({
      ticker,
      status,
    }: {
      ticker: string
      status: RequestStatus
    }) =>
      unwrap(
        api.PATCH('/admin/company-requests/{ticker}', {
          params: { path: { ticker } },
          body: { status },
        }),
      ),
    onSuccess: () =>
      Promise.all(
        [['company-suggestions'], ['company-requests']].map((queryKey) =>
          queryClient.invalidateQueries({ queryKey }),
        ),
      ),
  })
}
