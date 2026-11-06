import type { RouteObject } from 'react-router'
import { CallsPage } from './pages/CallsPage'
import { Layout } from './components/Layout'
import { AboutPage } from './pages/AboutPage'
import { AlertsPage } from './pages/AlertsPage'
import { ErrorPage } from './pages/ErrorPage'
import { CompaniesPage } from './pages/CompaniesPage'
import { CompanySuggestionsPage } from './pages/CompanySuggestionsPage'
import { CompanyPage } from './pages/CompanyPage'
import { EarningsCallPage } from './pages/EarningsCallPage'
import { FeedPage } from './pages/FeedPage'
import { FilingPage } from './pages/FilingPage'
import { InsiderTradesPage } from './pages/InsiderTradesPage'
import { NotFoundPage } from './pages/NotFoundPage'
import { SearchPage } from './pages/SearchPage'
import { SuggestCompanyPage } from './pages/SuggestCompanyPage'
import { UnsubscribePage } from './pages/UnsubscribePage'

export const routes: RouteObject[] = [
  {
    element: <Layout />,
    errorElement: <ErrorPage />,
    children: [
      { index: true, element: <FeedPage /> },
      { path: 'insiders', element: <InsiderTradesPage /> },
      { path: 'calls', element: <CallsPage /> },
      { path: 'calls/qa/:id', element: <EarningsCallPage /> },
      { path: 'search', element: <SearchPage /> },
      { path: 'companies', element: <CompaniesPage /> },
      { path: 'companies/:ticker', element: <CompanyPage /> },
      { path: 'suggest', element: <SuggestCompanyPage /> },
      { path: 'admin/suggestions', element: <CompanySuggestionsPage /> },
      { path: 'filings/:accessionNo', element: <FilingPage /> },
      { path: 'alerts', element: <AlertsPage /> },
      { path: 'unsubscribe', element: <UnsubscribePage /> },
      { path: 'about', element: <AboutPage /> },
      { path: '*', element: <NotFoundPage /> },
    ],
  },
]
