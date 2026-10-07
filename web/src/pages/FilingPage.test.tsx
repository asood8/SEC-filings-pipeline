import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { filingDetail } from '../test/data'
import { mockApi, renderRoute } from '../test/render'

describe('FilingPage', () => {
  it('shows the summary, 8-K items, and a link to the original', async () => {
    mockApi({ '/filings/0000320193-26-000001': () => filingDetail() })
    renderRoute('/filings/0000320193-26-000001')

    expect(
      await screen.findByRole('heading', { level: 1, name: /Kevan Parekh/ }),
    ).toBeInTheDocument()
    expect(screen.getByText('Effective January 1, 2027')).toBeInTheDocument()
    expect(screen.getByText(/Director or officer change/)).toBeInTheDocument()
    expect(
      screen.getByRole('link', { name: /original on sec.gov/ }),
    ).toHaveAttribute(
      'href',
      'https://www.sec.gov/Archives/edgar/data/320193/x.htm',
    )
    expect(screen.getByText(/Summarized by an AI model/)).toBeInTheDocument()
    expect(screen.queryByText(/Check these numbers/)).not.toBeInTheDocument()
  })

  it('flags numbers the grounding check could not find', async () => {
    const detail = filingDetail()
    mockApi({
      '/filings/0000320193-26-000001': () => ({
        ...detail,
        summary: { ...detail.summary!, ungrounded_numbers: ['9.9 billion'] },
      }),
    })
    renderRoute('/filings/0000320193-26-000001')
    expect(await screen.findByText(/Check these numbers/)).toHaveTextContent(
      '9.9 billion',
    )
  })

  it('explains a Form 4 filed about another company', async () => {
    mockApi({
      '/filings/x': () =>
        filingDetail({
          accession_no: 'x',
          cik: 1067983,
          ticker: 'BRK-B',
          company_name: 'BERKSHIRE HATHAWAY INC',
          form_type: '4',
          items: null,
          summary: null,
          form4: {
            period_of_report: '2026-09-17',
            issuer_cik: 920760,
            issuer_name: 'LENNAR CORP',
            issuer_ticker: 'LEN',
            reporting_owners: ['BERKSHIRE HATHAWAY INC', 'BUFFETT WARREN E'],
            transaction_codes: ['P'],
            is_10b5_1: false,
          },
        }),
    })
    renderRoute('/filings/x')

    const trade = (
      await screen.findByRole('heading', { name: 'Insider trade' })
    ).parentElement!
    expect(
      within(trade).getByRole('link', { name: 'LENNAR CORP' }),
    ).toHaveAttribute('href', '/companies/LEN')
    expect(within(trade).getByText(/filed by BERKSHIRE/)).toBeInTheDocument()
    expect(
      within(trade).getByText('Open-market or private purchase'),
    ).toBeInTheDocument()
    expect(
      within(trade).getByText('Not marked as a Rule 10b5-1 trading plan'),
    ).toBeInTheDocument()
    expect(screen.getByText(/hasn't been summarized yet/)).toBeInTheDocument()
  })

  it('shows not found for an unknown filing', async () => {
    mockApi({})
    renderRoute('/filings/nope')
    expect(
      await screen.findByRole('heading', { name: 'Not found' }),
    ).toBeInTheDocument()
  })
})
