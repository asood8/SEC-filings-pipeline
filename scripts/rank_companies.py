"""Rank US companies by a year's revenue from their own SEC filings, to choose watchlist additions.

    uv run python scripts/rank_companies.py [--count 100] [--year 2025] [--cache DIR]

Prints the largest companies not on config/watchlist.toml that trade on a US exchange, are
headquartered in the US (SEC's business address), file 10-Ks rather than a foreign issuer's 20-F
or 40-F (so they file 8-Ks), and have insiders filing Form 4s, less the few in EXCLUDED, which
were reviewed by hand. Then the larger companies it left out, and why.

Revenue is the largest of the usual us-gaap revenue tags for the calendar year, from SEC's XBRL
frames API, or the year before for a company without one yet (a new holding company). Banks are
counted by gross revenue, interest income plus noninterest income, as Fortune's list counts them;
many tag only net revenue. The result is close to the Fortune 500's order, not the same.

Every request goes through the shared EDGAR client (declared User-Agent, the Redis rate limit), so
Valkey must be running (`make up`): about 10 large frame files, then one company profile per
candidate until enough qualify. `--cache DIR` keeps the responses for later runs.
"""

import argparse
import json
import tomllib
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
WATCHLIST = REPO / "config" / "watchlist.toml"

REVENUE_TAGS = (
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "RevenuesNetOfInterestExpense",
)
BANK_INTEREST_TAGS = ("InterestAndDividendIncomeOperating", "InterestAndFeeIncomeLoansAndLeases")
BANK_OTHER_TAG = "NoninterestIncome"
US_EXCHANGES = frozenset({"NYSE", "Nasdaq", "NYSE American", "CBOE", "TXSE"})
# The 50 states and DC, as SEC's business addresses write them.
US_STATES = frozenset(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ "  # noqa: SIM905
    "NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split()
)
# Reviewed by hand (2026-11-18), by CIK.
EXCLUDED = {
    1855447: "its XBRL revenue is a scaling error (about $0.1B tagged as $103.5B)",  # Tigo Energy
    1070423: "the same business as Plains GP Holdings",  # Plains All American
    1552275: "the same business as SunocoCorp",  # Sunoco LP
    823277: "a cooperative; only preferred stock trades",  # CHS
    1527469: "owned by Apollo; only preferred stock trades",  # Athene
}

Frame = list[dict[str, Any]]  # a frame's "data": one row per company


def revenues(
    current: Mapping[str, Frame], previous: Mapping[str, Frame]
) -> dict[int, tuple[float, str]]:
    """Each company's revenue and name, by CIK, from the frames of the year (`current`) and the
    year before (`previous`), each keyed by tag. A company in the year's frames is never taken
    from the year before."""
    result: dict[int, tuple[float, str]] = {}
    for frames, fallback in ((current, False), (previous, True)):
        found: dict[int, tuple[float, str]] = {}
        for tag in REVENUE_TAGS:
            for row in frames.get(tag, []):
                if row["val"] > found.get(row["cik"], (0.0, ""))[0]:
                    found[row["cik"]] = (float(row["val"]), row["entityName"])
        interest: dict[int, float] = {}
        for tag in BANK_INTEREST_TAGS:
            for row in frames.get(tag, []):
                interest[row["cik"]] = max(interest.get(row["cik"], 0.0), float(row["val"]))
        for row in frames.get(BANK_OTHER_TAG, []):
            cik = row["cik"]
            gross = interest.get(cik, 0.0) + float(row["val"])
            if cik in interest and gross > found.get(cik, (0.0, ""))[0]:
                found[cik] = (gross, row["entityName"])
        for cik, value in found.items():
            if not (fallback and cik in result):
                result[cik] = value
    return result


def listed_tickers(tickers_exchange: Mapping[str, Any]) -> dict[int, list[str]]:
    """Tickers on a US exchange, by CIK, from SEC's company_tickers_exchange.json."""
    column = {name: i for i, name in enumerate(tickers_exchange["fields"])}
    result: dict[int, list[str]] = {}
    for row in tickers_exchange["data"]:
        if row[column["exchange"]] in US_EXCHANGES:
            result.setdefault(row[column["cik"]], []).append(row[column["ticker"]])
    return result


def profile(submissions: Mapping[str, Any]) -> dict[str, Any]:
    """What `reasons_left_out` needs from a company's submissions document."""
    return {
        "name": submissions.get("name"),
        "tickers": submissions.get("tickers", []),
        "business": (submissions.get("addresses") or {}).get("business") or {},
        "forms": sorted(set(submissions.get("filings", {}).get("recent", {}).get("form", []))),
    }


def reasons_left_out(cik: int, company: Mapping[str, Any]) -> list[str]:
    """Why a company doesn't qualify; empty if it does."""
    reasons = [EXCLUDED[cik]] if cik in EXCLUDED else []
    where = company["business"].get("stateOrCountry")
    if where not in US_STATES:
        reasons.append(f"headquartered outside the US ({company['business'].get('city')})")
    forms = set(company["forms"])
    if forms & {"20-F", "40-F"}:
        reasons.append("a foreign issuer (20-F or 40-F, so no 8-Ks)")
    if not forms & {"4", "4/A"}:
        reasons.append("no Form 4s")
    return reasons


def rank(
    revenue: Mapping[int, tuple[float, str]],
    listed: Mapping[int, list[str]],
    on_watchlist: set[int],
    company: Callable[[int], Mapping[str, Any]],
    count: int,
) -> tuple[list[tuple[int, float, Mapping[str, Any]]], list[tuple[int, float, str, list[str]]]]:
    """The `count` largest qualifying companies, as (cik, revenue, profile), and the larger ones
    left out, as (cik, revenue, name, reasons). Profiles are fetched only as far as needed."""
    chosen: list[tuple[int, float, Mapping[str, Any]]] = []
    left_out: list[tuple[int, float, str, list[str]]] = []
    candidates = sorted(
        (cik for cik in revenue if cik in listed and cik not in on_watchlist),
        key=lambda cik: -revenue[cik][0],
    )
    for cik in candidates:
        if len(chosen) == count:
            break
        details = company(cik)
        if reasons := reasons_left_out(cik, details):
            left_out.append((cik, revenue[cik][0], str(details["name"]), reasons))
        else:
            chosen.append((cik, revenue[cik][0], details))
    return chosen, left_out


def watchlist_ciks(listed: Mapping[int, list[str]], tickers: Iterable[str]) -> set[int]:
    wanted = set(tickers)
    return {cik for cik, symbols in listed.items() if wanted.intersection(symbols)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--cache", type=Path, help="keep SEC's responses here for later runs")
    args = parser.parse_args(argv)

    # Imported here, so the tests of the functions above need neither Redis nor the settings.
    import redis

    from filings_common.config import EdgarSettings, RedisSettings
    from filings_common.edgar import SEC_DATA, EdgarClient

    with EdgarClient.from_settings(
        EdgarSettings(), redis.Redis.from_url(RedisSettings().redis_url)
    ) as sec:

        def cached(name: str, fetch: Callable[[], Any]) -> Any:
            path = args.cache / name if args.cache else None
            if path is not None and path.exists():
                return json.loads(path.read_text())
            value = fetch()
            if path is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(value))
            return value

        def frame(tag: str, year: int) -> Frame:
            url = f"{SEC_DATA}/api/xbrl/frames/us-gaap/{tag}/USD/CY{year}.json"
            rows: Frame = cached(f"{tag}-CY{year}.json", lambda: sec.get_json(url))["data"]
            return rows

        def frames(year: int) -> dict[str, Frame]:
            tags = (*REVENUE_TAGS, *BANK_INTEREST_TAGS, BANK_OTHER_TAG)
            return {tag: frame(tag, year) for tag in tags}

        revenue = revenues(frames(args.year), frames(args.year - 1))
        listed = listed_tickers(cached("tickers.json", sec.company_tickers_exchange))
        on_watchlist = watchlist_ciks(listed, tomllib.loads(WATCHLIST.read_text())["tickers"])
        chosen, left_out = rank(
            revenue,
            listed,
            on_watchlist,
            lambda cik: cached(f"companies/{cik}.json", lambda: profile(sec.submissions(cik))),
            args.count,
        )

    print(f"The {len(chosen)} largest by {args.year} revenue not on the watchlist:")
    for position, (cik, value, details) in enumerate(chosen, start=1):
        ticker = next(t for t in details["tickers"] if t in listed[cik])
        print(f"{position:4}  {value / 1e9:6.1f}B  {ticker:6}  {details['name']}")
    print("\nLarger, but left out:")
    for cik, value, name, reasons in left_out:
        print(f"      {value / 1e9:6.1f}B  {cik:>8}  {name}: {'; '.join(reasons)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
