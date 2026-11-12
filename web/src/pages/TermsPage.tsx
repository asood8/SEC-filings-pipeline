import { Link } from 'react-router'
import { CONTACT_EMAIL, POLICIES_UPDATED, SITE_NAME } from '../lib/site'
import { usePageTitle } from '../lib/usePageTitle'
import styles from './AboutPage.module.css'

/** The conditions for using the site, in plain words. */
export function TermsPage() {
  usePageTitle('Terms')
  return (
    <article className={styles.page}>
      <h1>Terms of use</h1>
      <p className="muted">Last updated {POLICIES_UPDATED}.</p>
      <p>
        {SITE_NAME} is a free, personal project. By using it you agree to these
        terms. If you don&apos;t, please don&apos;t use it.
      </p>

      <h2>Not investment advice</h2>
      <p>
        The site summarizes public company filings for information only. Nothing
        on it, or in an alert it sends, is investment, financial, legal, or tax
        advice, or a recommendation to buy or sell anything. Decide for
        yourself, and read the original filing first.
      </p>

      <h2>Summaries can be wrong</h2>
      <p>
        Summaries are generated automatically, some by an AI language model.
        They can misstate or leave out facts, and alerts can be late or missing.
        Each filing links to the original on sec.gov, which is the authoritative
        version. See <Link to="/about">About</Link> for how they are made.
      </p>

      <h2>No warranty</h2>
      <p>
        The site is provided as it is, without warranties of any kind, and may
        change, break, or stop at any time. To the extent the law allows, its
        operator is not liable for any loss that comes from using it or relying
        on it.
      </p>

      <h2>Using it fairly</h2>
      <ul>
        <li>
          Don&apos;t try to break, overload, or get around the limits of the
          site or its API.
        </li>
        <li>
          Send alerts only to places you control or may post to: your own email,
          and Discord or Slack webhooks you were given.
        </li>
        <li>
          One account per person. Accounts that abuse the site may be removed.
        </li>
      </ul>

      <h2>Whose content it is</h2>
      <p>
        Filings belong to the companies that filed them and are public through
        the SEC. Company names and trademarks belong to their owners and are
        used only to identify them. The site isn&apos;t affiliated with or
        endorsed by the SEC, any company it covers, or Fortune. Its own code is
        open source under the MIT license.
      </p>

      <h2>Your account</h2>
      <p>
        You can delete your account at any time on the{' '}
        <Link to="/alerts">Alerts</Link> page. What the site keeps about you is
        described under <Link to="/privacy">Privacy</Link>.
      </p>

      <h2>Changes and contact</h2>
      <p>
        These terms may change; the date above shows when they last did.
        Questions: <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>.
      </p>
    </article>
  )
}
