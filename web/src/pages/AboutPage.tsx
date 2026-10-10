import { Link } from 'react-router'
import { ExternalLink } from '../components/ExternalLink'
import { SITE_NAME } from '../lib/site'
import { usePageTitle } from '../lib/usePageTitle'
import styles from './AboutPage.module.css'

export function AboutPage() {
  usePageTitle('About')
  return (
    <article className={styles.page}>
      <h1>About {SITE_NAME}</h1>
      <p>
        This site follows new SEC filings from a{' '}
        <Link to="/companies">watchlist of large US companies</Link>. It
        summarizes each filing in plain English, lets you{' '}
        <Link to="/search">search them</Link> by keyword or by meaning, and can{' '}
        <Link to="/alerts">alert you</Link> on Discord, Slack, or email when a
        company you follow files.
      </p>

      <h2>What it covers</h2>
      <ul>
        <li>
          <strong>Form 4</strong>: insider trades, where a company's officers,
          directors, and largest shareholders report buying, selling, or being
          given its shares.
        </li>
        <li>
          <strong>Form 8-K</strong>: current reports of major events, such as
          results, agreements, executive changes, and shareholder votes, with
          any press release attached.
        </li>
      </ul>
      <p>
        Other forms, such as annual and quarterly reports, and amended filings
        (4/A, 8-K/A) aren't collected yet. When a company joins the watchlist,
        its filings from the previous 30 days are collected; older ones aren't.
      </p>

      <h2>Where the data comes from</h2>
      <p>
        Every filing comes from{' '}
        <ExternalLink href="https://www.sec.gov/edgar/search/">
          SEC EDGAR
        </ExternalLink>
        , the US Securities and Exchange Commission's public database of company
        filings. Each filing's page links to the original on sec.gov, which is
        always the authoritative version. This site isn't affiliated with or
        endorsed by the SEC.
      </p>

      <h2>How fresh it is</h2>
      <p>
        The site checks EDGAR for new filings every 10 minutes. A Form 4's
        summary is ready seconds later; an 8-K's can take a few minutes. The
        first alert it sent, for an Apple Form 4 on 8 October 2026, reached
        Discord 8½ minutes after the SEC accepted the filing.
      </p>

      <h2>How summaries are written</h2>
      <p>
        Form 4 summaries are filled in from the filing's own data by a fixed
        template, so names, share counts, and prices appear as filed. Form 8-K
        summaries are written by a small AI language model, and can misstate or
        leave out facts. A check compares every number in an 8-K summary with
        the filing and lists any it can't find on the filing's page, but it
        can't catch every mistake.
      </p>

      <h2>Limits</h2>
      <ul>
        <li>
          Summaries are generated automatically and may contain errors. Read the
          original filing before relying on one.
        </li>
        <li>Nothing on this site is investment advice.</li>
      </ul>
    </article>
  )
}
