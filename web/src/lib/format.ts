const dateFormat = new Intl.DateTimeFormat('en-US', {
  year: 'numeric',
  month: 'short',
  day: 'numeric',
  timeZone: 'UTC',
})

const dateTimeFormat = new Intl.DateTimeFormat('en-US', {
  dateStyle: 'medium',
  timeStyle: 'short',
})

/** A date-only value like "2026-09-28". Formatted in UTC, so it never shifts a day. */
export function formatDate(isoDate: string): string {
  return dateFormat.format(new Date(`${isoDate}T00:00:00Z`))
}

/** The date `days` before today, in the reader's time zone, like "2026-09-28". */
export function isoDateDaysAgo(days: number, now = new Date()): string {
  const day = new Date(now.getFullYear(), now.getMonth(), now.getDate() - days)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${day.getFullYear()}-${pad(day.getMonth() + 1)}-${pad(day.getDate())}`
}

/** A timestamp, shown in the reader's time zone. */
export function formatDateTime(isoDateTime: string): string {
  return dateTimeFormat.format(new Date(isoDateTime))
}

const easternFormat = new Intl.DateTimeFormat('en-US', {
  dateStyle: 'medium',
  timeStyle: 'short',
  timeZone: 'America/New_York',
})

const easternClockFormat = new Intl.DateTimeFormat('en-US', {
  timeStyle: 'short',
  timeZone: 'America/New_York',
})

/** Only the time, on the market's clock: "5:30 PM ET". */
export function formatEasternClock(isoDateTime: string): string {
  return `${easternClockFormat.format(new Date(isoDateTime))} ET`
}

const weekdayFormat = new Intl.DateTimeFormat('en-US', {
  weekday: 'long',
  month: 'long',
  day: 'numeric',
  timeZone: 'UTC',
})

/** A date-only value with its weekday: "Wednesday, October 28". */
export function formatWeekday(isoDate: string): string {
  return weekdayFormat.format(new Date(`${isoDate}T00:00:00Z`))
}

/** A timestamp on the market's clock, as companies announce calls: "Oct 21, 2026, 5:30 PM ET". */
export function formatEasternTime(isoDateTime: string): string {
  return `${easternFormat.format(new Date(isoDateTime))} ET`
}

// Fractional shares exist (dividend reinvestment, RSU vesting), down to thousandths.
const sharesFormat = new Intl.NumberFormat('en-US', {
  maximumFractionDigits: 4,
})

const priceFormat = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
  maximumFractionDigits: 4,
})

const centsFormat = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
})

const dollarsFormat = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  maximumFractionDigits: 0,
})

export function formatShares(shares: number): string {
  return sharesFormat.format(shares)
}

/** A price per share, as filed: "$77.60", "$20.107". */
export function formatPrice(price: number): string {
  return priceFormat.format(price)
}

/** An amount of money: cents only below $1,000 ("$28.50", "$55,872"). */
export function formatMoney(amount: number): string {
  return (Math.abs(amount) < 1000 ? centsFormat : dollarsFormat).format(amount)
}

export function formLabel(formType: string): string {
  return formType === '4' ? 'Form 4' : formType
}

export function edgarCompanyUrl(cik: number): string {
  return `https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=${cik}`
}
