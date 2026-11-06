import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { callSummary } from '../test/data'
import { mockApi, renderRoute } from '../test/render'

describe('EarningsCallPage', () => {
  it('shows the takeaways and every exchange, linking the transcript', async () => {
    mockApi({ '/call-summaries/7': () => callSummary() })
    renderRoute('/calls/qa/7')

    expect(
      await screen.findByRole('heading', {
        level: 1,
        name: 'JPMorgan Chase: deposits, credit and the consumer',
      }),
    ).toBeInTheDocument()
    expect(document.title).toBe('JPM earnings call, Oct 13, 2026 · Filingwire')
    expect(
      screen.getByRole('link', { name: 'JPMorgan Chase & Co.' }),
    ).toHaveAttribute('href', '/companies/JPM')
    expect(
      screen.getByRole('link', {
        name: /Full transcript on www\.jpmorganchase\.com/,
      }),
    ).toHaveAttribute(
      'href',
      'https://www.jpmorganchase.com/ir/3q26-earnings-transcript.pdf',
    )
    const [answered, failed] = within(
      screen
        .getByRole('heading', { name: 'Questions and answers' })
        .closest('section')!,
    ).getAllByRole('listitem')
    expect(answered).toHaveTextContent('Matt O’Connor · Deutsche Bank')
    expect(answered).toHaveTextContent(
      'Answer: Jeremy Barnum said 650,000 net new checking accounts',
    )
    expect(failed).toHaveTextContent("This question couldn't be summarized.")
    expect(
      screen.getByText(/One question couldn't be summarized/),
    ).toBeInTheDocument()
    expect(screen.getByText(/Summarized by an AI model/)).toHaveTextContent(
      'llama3.2:3b',
    )
  })

  it('flags numbers and names the transcript does not have', async () => {
    mockApi({
      '/call-summaries/7': () =>
        callSummary({
          ungrounded_numbers: ['9.9 billion'],
          ungrounded_names: ['Whitfield'],
        }),
    })
    renderRoute('/calls/qa/7')
    expect(
      await screen.findByText(/Check these numbers against the transcript/),
    ).toHaveTextContent('9.9 billion')
    expect(
      screen.getByText(/Check these names against the transcript/),
    ).toHaveTextContent('Whitfield')
  })

  it('says when there is no such call', async () => {
    mockApi({})
    renderRoute('/calls/qa/999')
    expect(
      await screen.findByRole('heading', { name: 'Not found' }),
    ).toBeInTheDocument()
    expect(screen.getByText(/That call doesn't exist here/)).toBeInTheDocument()
  })
})
