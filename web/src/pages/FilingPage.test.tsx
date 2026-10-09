import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import {
  BERKSHIRE,
  BUFFETT,
  filingDetail,
  form4Transaction,
  TANEJA,
  withoutRelationship,
} from '../test/data'
import { mockApi, renderRoute } from '../test/render'
import { rowText } from '../test/table'

describe('FilingPage', () => {
  it('shows the summary, 8-K items, and a link to the original', async () => {
    mockApi({ '/filings/0000320193-26-000001': () => filingDetail() })
    renderRoute('/filings/0000320193-26-000001')

    expect(
      await screen.findByRole('heading', { level: 1, name: /Kevan Parekh/ }),
    ).toBeInTheDocument()
    expect(screen.getByText('Effective January 1, 2027')).toBeInTheDocument()
    expect(screen.getByText(/Director or officer change/)).toBeInTheDocument()
    const original = screen.getByRole('link', {
      name: 'Read the original on sec.gov (opens in a new tab)',
    })
    expect(original).toHaveAttribute(
      'href',
      'https://www.sec.gov/Archives/edgar/data/320193/x.htm',
    )
    expect(original).toHaveAttribute('target', '_blank')
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
            reporting_owners: [BERKSHIRE, BUFFETT],
            transaction_codes: ['P'],
            is_10b5_1: false,
            transactions: [], // parsed before transaction lines were stored
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
      within(trade).getByText('Reported by').nextElementSibling,
    ).toHaveTextContent(
      'Berkshire Hathaway Inc (10% owner); Warren E Buffett (10% owner)',
    )
    expect(
      within(trade).getByText('Open-market or private purchase'),
    ).toBeInTheDocument()
    expect(within(trade).queryByRole('table')).not.toBeInTheDocument()
    expect(
      within(trade).getByText('Not marked as a Rule 10b5-1 trading plan'),
    ).toBeInTheDocument()
    expect(screen.getByText(/hasn't been summarized yet/)).toBeInTheDocument()
  })

  it('says who reported the trade, with the title as filed on hover', async () => {
    mockApi({
      '/filings/x': () =>
        filingDetail({
          accession_no: 'x',
          cik: 1318605,
          ticker: 'TSLA',
          company_name: 'Tesla, Inc.',
          form_type: '4',
          items: null,
          form4: {
            period_of_report: '2026-09-15',
            issuer_cik: 1318605,
            issuer_name: 'Tesla, Inc.',
            issuer_ticker: 'TSLA',
            reporting_owners: [TANEJA],
            transaction_codes: ['M', 'S'],
            is_10b5_1: true,
            transactions: [form4Transaction()],
          },
        }),
    })
    renderRoute('/filings/x')

    const reporter = (await screen.findByText('Reported by'))
      .nextElementSibling as HTMLElement
    expect(reporter).toHaveTextContent(/^Vaibhav Taneja \(CFO\)$/)
    expect(within(reporter).getByText('(CFO)')).toHaveAttribute(
      'title',
      'Title as filed: Chief Financial Officer',
    )
  })

  it("lists a Form 4's transactions in plain language", async () => {
    mockApi({
      '/filings/x': () =>
        filingDetail({
          accession_no: 'x',
          form_type: '4',
          items: null,
          form4: {
            period_of_report: '2026-09-08',
            issuer_cik: 320193,
            issuer_name: 'Apple Inc.',
            issuer_ticker: 'AAPL',
            // Saved before owner CIKs and relationships were stored.
            reporting_owners: [
              withoutRelationship({
                cik: null,
                name: 'BARTON RICHARD N',
                display_name: 'Richard N Barton',
              }),
            ],
            transaction_codes: ['M', 'S'],
            is_10b5_1: true,
            transactions: [
              form4Transaction(),
              form4Transaction({
                line: 2,
                code: 'M',
                shares: 1060,
                price_per_share: null,
                acquired_disposed: 'A',
                shares_owned_after: 63388,
                direct_or_indirect: 'I',
              }),
              form4Transaction({
                line: 3,
                is_derivative: true,
                security_title: 'Non-Qualified Stock Option (right to buy)',
                code: 'M',
                price_per_share: 0,
                shares_owned_after: 230,
              }),
            ],
          },
        }),
    })
    renderRoute('/filings/x')

    const table = await screen.findByRole('table', {
      name: 'Transactions reported in this filing',
    })
    // A wide table scrolls inside a region keyboard users can focus and scroll.
    expect(
      screen.getByRole('region', {
        name: 'Transactions reported in this filing',
      }),
    ).toHaveAttribute('tabindex', '0')
    expect(within(table).getAllByRole('columnheader')).toHaveLength(8)
    const [, sale, exercise, option] = within(table).getAllByRole('row')
    expect(within(sale).getByRole('rowheader')).toHaveTextContent(
      'Open-market or private sale',
    )
    expect(rowText(sale)).toEqual([
      'Open-market or private sale',
      'Sep 8, 2026',
      'Common Stock',
      'disposed of −720',
      '$77.60',
      '$55,872',
      '2,460',
      'Direct',
    ])
    expect(rowText(exercise)).toEqual([
      'Exercise or conversion of a derivative',
      'Sep 8, 2026',
      'Common Stock',
      'acquired +1,060',
      '—', // price only in a footnote
      '—',
      '63,388',
      'Indirect',
    ])
    expect(within(option).getByText('Derivative')).toBeInTheDocument()
    expect(rowText(option).slice(3, 6)).toEqual([
      'disposed of −720',
      '$0.00',
      '$0.00',
    ])
    expect(
      screen.getByText(/gives the price only in a footnote/),
    ).toBeInTheDocument()
    // No relationship is known for an owner saved before they were stored.
    expect(
      screen.getByText('Reported by').nextElementSibling,
    ).toHaveTextContent(/^Richard N Barton$/)
    // The table replaces the list of codes.
    expect(
      screen.queryByText('Transactions', { selector: 'dt' }),
    ).not.toBeInTheDocument()
  })

  it('shows not found for an unknown filing', async () => {
    mockApi({})
    renderRoute('/filings/nope')
    expect(
      await screen.findByRole('heading', { name: 'Not found' }),
    ).toBeInTheDocument()
  })
})
