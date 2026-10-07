import type { RouteObject } from 'react-router'
import { Layout } from './components/Layout'
import { CompaniesPage } from './pages/CompaniesPage'
import { CompanyPage } from './pages/CompanyPage'
import { FeedPage } from './pages/FeedPage'
import { FilingPage } from './pages/FilingPage'
import { NotFoundPage } from './pages/NotFoundPage'
import { SearchPage } from './pages/SearchPage'

export const routes: RouteObject[] = [
  {
    element: <Layout />,
    children: [
      { index: true, element: <FeedPage /> },
      { path: 'search', element: <SearchPage /> },
      { path: 'companies', element: <CompaniesPage /> },
      { path: 'companies/:ticker', element: <CompanyPage /> },
      { path: 'filings/:accessionNo', element: <FilingPage /> },
      { path: '*', element: <NotFoundPage /> },
    ],
  },
]
