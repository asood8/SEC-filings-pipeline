import createClient from 'openapi-fetch'
import type { components, paths } from './schema'

type Schemas = components['schemas']
export type Filing = Schemas['FilingOut']
export type FilingDetail = Schemas['FilingDetail']
export type Form4Transaction = Schemas['Form4TransactionOut']
export type InsiderActivity = Schemas['InsiderActivity']
export type TradeTotals = Schemas['TradeTotals']
export type SearchResult = Schemas['SearchResultOut']
export type Company = Schemas['CompanyOut']
export type User = Schemas['UserOut']
export type Subscription = Schemas['SubscriptionOut']
export type NewSubscription = Schemas['SubscriptionCreate']
export type SubscriptionBatch = Schemas['SubscriptionBatch']
export type Destination = Schemas['Destination']
export type SubscriptionUpdate = Schemas['SubscriptionUpdate']
export type Channel = Schemas['Channel']

export const API_BASE_URL: string =
  import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'

export const api = createClient<paths>({
  baseUrl: API_BASE_URL,
  // Sends the session cookie once sign-in exists.
  credentials: 'include',
  // Resolved on each request, not captured once, so tests can stub fetch.
  fetch: (request) => globalThis.fetch(request),
})

export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

export function isNotFound(error: Error | null): boolean {
  return error instanceof ApiError && error.status === 404
}

function errorMessage(status: number, error: unknown): string {
  if (status === 404) return 'Not found.'
  if (status === 429) return 'Too many requests. Wait a moment and try again.'
  if (status >= 500) return 'The server had a problem. Try again later.'
  const detail = (error as { detail?: unknown } | undefined)?.detail
  if (typeof detail === 'string') return capitalize(detail)
  // Request validation errors: a list of { msg } objects.
  if (Array.isArray(detail) && detail.length > 0) {
    return detail.map((item: { msg?: string }) => item.msg ?? '').join('; ')
  }
  return `Request failed (${status}).`
}

function capitalize(text: string): string {
  const sentence = text.charAt(0).toUpperCase() + text.slice(1)
  return /[.!?]$/.test(sentence) ? sentence : `${sentence}.`
}

type Result<T> = { data?: T; error?: unknown; response: Response }

/** openapi-fetch resolves to `{ data } | { error }`; queries want the data or an exception. */
export async function unwrap<T>(
  request: Promise<Result<T>>,
): Promise<Exclude<T, undefined>> {
  const { data, error, response } = await request
  if (error !== undefined || data === undefined) {
    throw new ApiError(response.status, errorMessage(response.status, error))
  }
  return data as Exclude<T, undefined>
}

/** Retry network and server errors, never client errors (404, 422) or rate limits. */
export function shouldRetry(failureCount: number, error: Error): boolean {
  if (error instanceof ApiError && error.status < 500) return false
  return failureCount < 2
}
