import {
  queryOptions,
  useMutation,
  useQueryClient,
} from '@tanstack/react-query'
import {
  api,
  ApiError,
  unwrap,
  type SubscriptionBatch,
  type SubscriptionUpdate,
} from './client'

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

export function useSignIn() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (credential: string) =>
      unwrap(api.POST('/auth/google', { body: { credential } })),
    onSuccess: (user) => {
      queryClient.setQueryData(meQuery().queryKey, user)
      return queryClient.invalidateQueries({ queryKey: ['subscriptions'] })
    },
  })
}

function useSignedOut() {
  const queryClient = useQueryClient()
  return () => {
    queryClient.setQueryData(meQuery().queryKey, null)
    queryClient.removeQueries({ queryKey: ['subscriptions'] })
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
