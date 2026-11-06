"""`filings-admin grant|revoke <email>`: who reviews company suggestions on the website.

Run where the API runs (`kubectl -n filings exec deploy/api -- filings-admin grant <email>`).
The account must have signed in once. Admin rights are never granted through the API, so no
email address needs to be in the public repository's config.
"""

import argparse
import sys

from filings_api.routes.company_requests import grant
from filings_common.config import DatabaseSettings
from filings_common.db import create_engine, session_factory
from filings_common.targets import mask_email


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="filings-admin", description=__doc__.split("\n\n")[0])
    parser.add_argument("action", choices=["grant", "revoke"])
    parser.add_argument("email", help="the account's Google email address")
    args = parser.parse_args(argv)
    engine = create_engine(DatabaseSettings().database_url.get_secret_value())
    try:
        with session_factory(engine)() as session:
            changed = grant(session, args.email, admin=args.action == "grant")
    finally:
        engine.dispose()
    who = mask_email(args.email.strip().lower())
    if not changed:
        print(f"no account for {who}: sign in on the website first", file=sys.stderr)
        return 1
    print(f"{who} is {'now' if args.action == 'grant' else 'no longer'} an admin")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
