import type {
  Filing,
  FilingDetail,
  Form4Transaction,
  InsiderActivity,
  ReportingOwner,
} from '../api/client'

// Form 4 reporting owners as the API returns them (real names and CIKs from EDGAR).
export const COOK: ReportingOwner = {
  cik: 1214156,
  name: 'COOK TIMOTHY D',
  display_name: 'Timothy D Cook',
}
export const BERKSHIRE: ReportingOwner = {
  cik: 1067983,
  name: 'BERKSHIRE HATHAWAY INC',
  display_name: 'Berkshire Hathaway Inc',
}
export const BUFFETT: ReportingOwner = {
  cik: 315090,
  name: 'BUFFETT WARREN E',
  display_name: 'Warren E Buffett',
}

export function filing(overrides: Partial<Filing> = {}): Filing {
  return {
    accession_no: '0000320193-26-000001',
    cik: 320193,
    ticker: 'AAPL',
    company_name: 'Apple Inc.',
    form_type: '8-K',
    filed_at: '2026-09-28',
    accepted_at: '2026-09-28T20:05:00Z',
    status: 'ready',
    headline: 'Apple names Kevan Parekh chief financial officer',
    sec_url: 'https://www.sec.gov/Archives/edgar/data/320193/x.htm',
    ...overrides,
  }
}

export function filingDetail(
  overrides: Partial<FilingDetail> = {},
): FilingDetail {
  return {
    ...filing(),
    summary: {
      headline: 'Apple names Kevan Parekh chief financial officer',
      summary: 'Apple appointed a new CFO effective January 1.',
      key_points: ['Effective January 1, 2027'],
      provider: 'gemini',
      model: 'gemini-3.5-flash-lite',
      prompt_version: 1,
      created_at: '2026-09-28T21:00:00Z',
      ungrounded_numbers: [],
    },
    form4: null,
    items: ['5.02', '9.01'],
    exhibits: [],
    ...overrides,
  }
}

/** An open-market sale; override fields for other kinds. */
export function form4Transaction(
  overrides: Partial<Form4Transaction> = {},
): Form4Transaction {
  return {
    line: 1,
    is_derivative: false,
    security_title: 'Common Stock',
    transaction_date: '2026-09-08',
    code: 'S',
    shares: 720,
    price_per_share: 77.6,
    acquired_disposed: 'D',
    shares_owned_after: 2460,
    direct_or_indirect: 'D',
    ...overrides,
  }
}

const NO_TRADES = { shares: 0, value: 0, unpriced_shares: 0 }

export function insiderActivity(
  overrides: Partial<InsiderActivity> = {},
): InsiderActivity {
  return {
    ticker: 'AAPL',
    days: 90,
    since: '2026-07-10',
    bought: NO_TRADES,
    sold: NO_TRADES,
    filings: 0,
    owners: [],
    ...overrides,
  }
}
