import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { vi } from 'vitest'
import { routes } from '../routes'

type Handler = (url: URL) => unknown

/** Stubs fetch with canned API responses, keyed by path ("/filings/123"). A handler's return
 * value is sent as JSON; return a Response for anything else. Unknown paths get a 404. */
export function mockApi(handlers: Record<string, Handler>) {
  const requests: URL[] = []
  const fetch = vi.fn(async (request: Request) => {
    const url = new URL(request.url)
    requests.push(url)
    const handler = handlers[url.pathname]
    if (!handler) return Response.json({ detail: 'Not Found' }, { status: 404 })
    const body = handler(url)
    return body instanceof Response ? body : Response.json(body)
  })
  vi.stubGlobal('fetch', fetch)
  return requests
}

export function renderRoute(path: string) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  const router = createMemoryRouter(routes, { initialEntries: [path] })
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return router
}
