import type { Filing, FilingDetail } from '../api/client'

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
