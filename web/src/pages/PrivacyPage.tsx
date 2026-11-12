import { Link } from 'react-router'
import { CONTACT_EMAIL, POLICIES_UPDATED, SITE_NAME } from '../lib/site'
import { usePageTitle } from '../lib/usePageTitle'
import styles from './AboutPage.module.css'

/** What the site keeps about a visitor and an account, in plain words. It describes what the
 * code does (ADR-0013, ADR-0012, ADR-0016): change it when that changes. */
export function PrivacyPage() {
  usePageTitle('Privacy')
  return (
    <article className={styles.page}>
      <h1>Privacy</h1>
      <p className="muted">Last updated {POLICIES_UPDATED}.</p>
      <p>
        {SITE_NAME} is a personal project run by one person. It keeps as little
        about you as it can, shows no ads, uses no analytics or tracking
        scripts, and never sells or shares your details for marketing.
      </p>

      <h2>If you only read the site</h2>
      <p>
        No account is needed to read filings or search. Like every website, the
        servers see your IP address and what you request. That is used to limit
        abuse and to find faults, kept in logs for up to 14 days, and not
        combined with anything else. Searches are not linked to you.
      </p>

      <h2>If you sign in</h2>
      <p>Signing in with Google creates an account. It holds:</p>
      <ul>
        <li>
          your email address and Google&apos;s identifier for your account. The
          site never sees your Google password, contacts, or anything else in
          your Google account;
        </li>
        <li>
          the alerts you set up: the companies and filters, and where to send
          them (a Discord or Slack webhook address, or your email);
        </li>
        <li>a record of which alerts were sent to you, so none repeats;</li>
        <li>any company you suggested, with the note you wrote;</li>
        <li>
          your sign-in sessions. Each lasts 7 days, and only a scrambled form of
          its key is stored.
        </li>
      </ul>

      <h2>Cookies</h2>
      <p>
        One cookie, set when you sign in, keeps you signed in. Google&apos;s
        sign-in button may set its own. There are no advertising or analytics
        cookies.
      </p>

      <h2>Who else handles it</h2>
      <p>
        The site runs on other companies&apos; services, which process data for
        it:
      </p>
      <ul>
        <li>
          Cloudflare delivers the website and carries requests to the server;
        </li>
        <li>
          Oracle Cloud hosts the server and the database that holds accounts;
        </li>
        <li>Google verifies who you are when you sign in;</li>
        <li>
          Grafana Cloud receives the servers&apos; technical logs and
          measurements (which include IP addresses, not account contents);
        </li>
        <li>
          Discord, Slack, or your email provider receive the alerts you asked to
          be sent there.
        </li>
      </ul>
      <p>
        Summaries are written from public filings only. Nothing about you is
        sent to an AI model.
      </p>

      <h2>Seeing, changing, and deleting it</h2>
      <p>
        Your alerts and suggestions are listed on the{' '}
        <Link to="/alerts">Alerts</Link> and{' '}
        <Link to="/suggest">Suggest a company</Link> pages. &ldquo;Delete your
        account&rdquo; on the Alerts page removes your account, every alert,
        your alert history, and your suggestions at once. Backups made before
        then are deleted within 30 days. Every alert email has an unsubscribe
        link.
      </p>

      <h2>Children</h2>
      <p>The site isn&apos;t meant for children under 13.</p>

      <h2>Questions and changes</h2>
      <p>
        Write to <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>. If
        this page changes in a way that matters, the date above changes and the
        change is described here. See also the <Link to="/terms">terms</Link>.
      </p>
    </article>
  )
}
