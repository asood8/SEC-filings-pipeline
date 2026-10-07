import { ApiError } from '../api/client'

export function Loading({ label = 'Loading…' }: { label?: string }) {
  return (
    <p className="muted" role="status">
      {label}
    </p>
  )
}

export function ErrorMessage({ error }: { error: Error }) {
  const message =
    error instanceof ApiError
      ? error.message
      : "Couldn't reach the API. Check your connection and try again."
  return (
    <p className="notice" role="alert">
      {message}
    </p>
  )
}
