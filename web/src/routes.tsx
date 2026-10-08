import type { RouteObject } from 'react-router'
import { Layout } from './components/Layout'
import { AlertsPage } from './pages/AlertsPage'
import { ErrorPage } from './pages/ErrorPage'
import { CompaniesPage } from './pages/CompaniesPage'
import { CompanyPage } from './pages/CompanyPage'
import { FeedPage } from './pages/FeedPage'
import { FilingPage } from './pages/FilingPage'
import { InsiderTradesPage } from './pages/InsiderTradesPage'
import { NotFoundPage } from './pages/NotFoundPage'
import { SearchPage } from './pages/SearchPage'

export const routes: RouteObject[] = [
  {
    element: <Layout />,
    errorElement: <ErrorPage />,
    children: [
      { index: true, element: <FeedPage /> },
      { path: 'insiders', element: <InsiderTradesPage /> },
      { path: 'search', element: <SearchPage /> },
      { path: 'companies', element: <CompaniesPage /> },
      { path: 'companies/:ticker', element: <CompanyPage /> },
      { path: 'filings/:accessionNo', element: <FilingPage /> },
      { path: 'alerts', element: <AlertsPage /> },
      { path: '*', element: <NotFoundPage /> },
    ],
  },
]
