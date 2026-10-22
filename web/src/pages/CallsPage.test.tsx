import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { API_BASE_URL, type UpcomingCall } from '../api/client'
import { mockApi, renderRoute } from '../test/render'

const MSFT: UpcomingCall = {
  kind: 'Earnings call',
  day: '2026-10-28',
  starts_at: '2026-10-28T14:30:00-07:00', // as filed: 2:30 p.m. Pacific
  webcast_url: 'https://www.microsoft.com/investor',
  ticker: 'MSFT',
  company_name: 'Microsoft Corp',
  accession_no: '0000789019-26-000003',
}
const ADBE: UpcomingCall = {
  kind: 'Earnings call',
  day: '2026-10-28',
  starts_at: null,
  webcast_url: null,
  ticker: 'ADBE',
  company_name: 'Adobe Inc.',
  accession_no: '0000796343-26-000144',
}
const TSLA: UpcomingCall = {
  kind: 'Q&A session',
  day: '2026-10-29',
  starts_at: '2026-10-29T16:30:00-05:00',
  webcast_url: 'https://ir.tesla.com',
  ticker: 'TSLA',
  company_name: 'Tesla, Inc.',
  accession_no: '0001628280-26-064366',
}

function answer(items: UpcomingCall[]) {
  return { since: '2026-10-22', until: '2026-12-21', items }
}

describe('CallsPage', () => {
  it('lists calls by day on Eastern time, with links and a calendar feed', async () => {
    mockApi({ '/calls': () => answer([MSFT, ADBE, TSLA]) })
    renderRoute('/calls')

    expect(
      await screen.findByRole('heading', { level: 1, name: 'Upcoming calls' }),
    ).toBeInTheDocument()
    expect(document.title).toBe('Upcoming calls · SEC Filings')
    const days = await screen.findAllByRole('heading', { level: 2 })
    expect(days.map((h) => h.textContent)).toEqual([
      'Wednesday, October 28',
      'Thursday, October 29',
    ])

    const wednesday = days[0].closest('section')!
    const [msft, adbe] = within(wednesday).getAllByRole('listitem')
    expect(within(msft).getByText('5:30 PM ET')).toHaveAttribute(
      'dateTime',
      '2026-10-28T14:30:00-07:00',
    )
    expect(within(msft).getByRole('link', { name: 'MSFT' })).toHaveAttribute(
      'href',
      '/companies/MSFT',
    )
    expect(
      within(msft).getByRole('link', { name: 'Webcast (opens in a new tab)' }),
    ).toHaveAttribute('href', 'https://www.microsoft.com/investor')
    expect(within(msft).getByRole('link', { name: 'Filing' })).toHaveAttribute(
      'href',
      '/filings/0000789019-26-000003',
    )
    expect(within(adbe).getByText('Time not given')).toBeInTheDocument()
    expect(within(adbe).queryByRole('link', { name: /Webcast/ })).toBeNull()

    expect(
      screen.getByRole('link', { name: 'Calendar feed (opens in a new tab)' }),
    ).toHaveAttribute('href', `${API_BASE_URL}/calls.ics`)
  })

  it('says so when no calls are announced', async () => {
    mockApi({ '/calls': () => answer([]) })
    renderRoute('/calls')
    expect(
      await screen.findByText('No calls announced for the next 60 days.'),
    ).toBeInTheDocument()
  })
})
