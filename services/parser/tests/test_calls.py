"""Upcoming investor calls in real 8-Ks: the fixtures in tests/fixtures/form8k/, and sentences
quoted from real press releases too large to keep as fixtures (Oracle's and PepsiCo's are over a
megabyte each)."""

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from filings_common.parsed import Exhibit, Form8K, InvestorCall, Item
from filings_parser.calls import find_call
from filings_parser.documents import RawExhibit
from filings_parser.form8k import parse

FIXTURES = Path(__file__).parents[3] / "tests" / "fixtures" / "form8k"


def fixture(accession_no: str) -> Form8K:
    exhibits = [
        RawExhibit("EX-99.1", path.name, "EX-99.1", path.read_bytes())
        for path in sorted((FIXTURES / "exhibits" / accession_no).glob("*"))
    ]
    return Form8K.model_validate(parse((FIXTURES / f"{accession_no}.htm").read_bytes(), exhibits))


def release(*paragraphs: str) -> Form8K:
    return Form8K(
        items=[Item(number="2.02", title="Results of Operations and Financial Condition", text="")],
        exhibits=[Exhibit(type="EX-99.1", description=None, text="\n".join(paragraphs))],
    )


def utc(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def test_q_and_a_webcast_on_that_day_with_the_link_lines_below() -> None:
    # Tesla's Q3 deliveries release announces the results webcast, then lists When/Time/Webcast.
    call = find_call(fixture("0001628280-26-064366"), date(2026, 10, 2))
    assert call is not None
    assert (call.kind, call.day, call.webcast_url) == (
        "Q&A session",
        date(2026, 10, 21),
        "https://ir.tesla.com",
    )
    assert call.starts_at == utc(2026, 10, 21, 21, 30)  # 4:30 p.m. Central Time
    assert call.describe() == "Q&A session: Oct 21, 2026, 5:30 pm ET, https://ir.tesla.com"


def test_conference_talk_dated_in_the_sentence_before_its_webcast() -> None:
    call = find_call(fixture("0000731766-26-000205"), date(2026, 9, 8))  # UnitedHealth, item 7.01
    assert call == InvestorCall(
        kind="Webcast",
        day=date(2026, 9, 9),
        starts_at=utc(2026, 9, 9, 12, 0),  # "beginning at 8:00 a.m., Eastern Time"
        webcast_url="https://www.unitedhealthgroup.com",
    )


def test_day_without_a_year_or_a_time_and_no_link() -> None:
    # Adobe, a week ahead: "hosting its Q3 FY2026 Earnings call on Thursday, September 10".
    call = find_call(fixture("0000796343-26-000144"), date(2026, 9, 8))
    assert call == InvestorCall(kind="Earnings call", day=date(2026, 9, 10))
    assert call.describe() == "Earnings call: Sep 10, 2026"


def test_investor_day_and_not_the_boilerplate_about_future_calls() -> None:
    call = find_call(fixture("0000063908-26-000076"), date(2026, 9, 23))  # McDonald's
    assert call == InvestorCall(
        kind="Investor Day",
        day=date(2026, 9, 23),
        starts_at=utc(2026, 9, 23, 13, 30),  # 8:30 a.m. (Central Time)
        webcast_url="https://www.investor.mcdonalds.com",
    )


def test_an_investor_day_already_held_is_not_a_call() -> None:
    # Salesforce filed its Investor Day slides the evening after: "posted ... used in the
    # Company's September 16, 2026 Investor Day event". The slides' agenda has "Leadership Q&A".
    assert find_call(fixture("0001108524-26-000210"), date(2026, 9, 16)) is None


@pytest.mark.parametrize(
    "accession_no",
    [
        "0000002488-26-000182",  # unregistered sales
        "0000320187-26-000143",  # officer change and vote results
        "0000320187-26-000184",  # Nike results; the call is in the press release, not a fixture
        "0001018724-26-000036",  # officer change
        "0001104659-26-107526",  # other events
        "0001628280-26-063820",  # credit agreement
    ],
)
def test_filings_that_announce_no_call(accession_no: str) -> None:
    assert find_call(fixture(accession_no), date(2026, 9, 1)) is None


def test_today_at_a_central_time_with_the_link_in_the_next_sentence() -> None:
    form = release(
        "Earnings Conference Call and Webcast",
        "Oracle will hold a conference call and webcast today to discuss these results at 4:00 "
        "p.m. Central. A live and replay webcast will be available on the Oracle Investor "
        "Relations website at www.oracle.com/investor/.",
    )
    assert find_call(form, date(2026, 9, 10)) == InvestorCall(
        kind="Conference call",
        day=date(2026, 9, 10),
        starts_at=utc(2026, 9, 10, 21, 0),
        webcast_url="https://www.oracle.com/investor/",
    )


def test_the_call_time_not_the_time_remarks_are_posted() -> None:
    form = release(
        "Prepared Management Remarks and Live Question and Answer Webcast",
        "At approximately 6:00 a.m. (Eastern time) on October 8, 2026, the Company will post "
        "prepared management remarks (in pdf format) regarding its third quarter results and "
        "business update, including its outlook for 2026, at https://www.pepsico.com/investors. "
        "At 8:15 a.m. (Eastern time) on October 8, 2026, the Company will host a live question "
        "and answer session with investors and financial analysts. Further details will be "
        "accessible on the Company’s website at https://www.pepsico.com/investors.",  # noqa: RUF001 (as filed)
    )
    # Accepted the evening before, at 5:57 p.m. ET (so its filing date is October 8).
    call = find_call(form, date(2026, 10, 7))
    assert call is not None
    assert (call.kind, call.starts_at) == ("Q&A session", utc(2026, 10, 8, 12, 15))
    assert call.webcast_url == "https://www.pepsico.com/investors"


def test_pacific_time_bare_investor_domain_and_not_the_replay_date() -> None:
    nike = release(
        "NIKE, Inc. management will host a conference call beginning at approximately 2:00 p.m. "
        "PT on October 1, 2026, to review fiscal first quarter results. The conference call will "
        "be broadcast live via the Internet and can be accessed at http://investors.nike.com. For "
        "those unable to listen to the live broadcast, an archived version will be available at "
        "the same location through approximately 9:00 p.m. PT, October 29, 2026.",
    )
    assert find_call(nike, date(2026, 10, 1)) == InvestorCall(
        kind="Conference call",
        day=date(2026, 10, 1),
        starts_at=utc(2026, 10, 1, 21, 0),
        webcast_url="http://investors.nike.com",
    )
    costco = release(
        "A conference call to discuss these results is scheduled for 2:00 p.m. (PT) today, "
        "September 24, 2026, and will be available via a webcast on investor.costco.com (click "
        "“Events & Presentations”).",
    )
    call = find_call(costco, date(2026, 9, 24))
    assert call is not None
    assert (call.starts_at, call.webcast_url) == (
        utc(2026, 9, 24, 21, 0),
        "https://investor.costco.com",
    )


def test_earnings_conference_call_today() -> None:
    adobe = release(
        "Adobe will webcast its third quarter fiscal year 2026 earnings conference call today at "
        "2:00 p.m. Pacific Time from its investor relations website: http://www.adobe.com/ADBE.",
    )
    assert find_call(adobe, date(2026, 9, 10)) == InvestorCall(
        kind="Earnings call",
        day=date(2026, 9, 10),
        starts_at=utc(2026, 9, 10, 21, 0),
        webcast_url="http://www.adobe.com/ADBE",
    )


# The rules, on made-up sentences.


def test_a_time_without_a_time_zone_gives_only_the_day() -> None:
    form = release("The company will host a conference call on October 20, 2026 at 5:00 p.m.")
    assert find_call(form, date(2026, 10, 1)) == InvestorCall(
        kind="Conference call", day=date(2026, 10, 20)
    )


def test_tomorrow_and_a_year_inferred_into_the_next_year() -> None:
    tomorrow = release("The company will hold an earnings call tomorrow at 8:30 a.m. ET.")
    call = find_call(tomorrow, date(2026, 12, 31))
    assert call is not None
    assert call.starts_at == utc(2027, 1, 1, 13, 30)
    january = release("The company will host its earnings call on January 28 at 4:30 p.m. ET.")
    call = find_call(january, date(2026, 12, 15))
    assert call is not None
    assert call.day == date(2027, 1, 28)


@pytest.mark.parametrize(
    "sentence",
    [
        # The weekday doesn't match the date: one of them is wrong.
        "The company will host a conference call on Friday, October 21, 2026.",
        # Before the release, or too far after it.
        "The company will host a conference call on September 1, 2026 at 5:00 p.m. ET.",
        "The company will host a conference call on June 1, 2027 at 5:00 p.m. ET.",
        # Not looking ahead, a replay, or no date at all.
        "The company held a conference call on October 21, 2026.",
        "A replay of the webcast will be available until October 21, 2026.",
        "The company will host a conference call to discuss its results.",
        # A date that doesn't exist.
        "The company will host a conference call on February 30, 2027.",
    ],
)
def test_no_call(sentence: str) -> None:
    assert find_call(release(sentence), date(2026, 10, 1)) is None


def test_items_come_before_exhibits() -> None:
    form = release("The company will host a Q&A session on October 30, 2026.")
    form.items[0].text = "The company will host a conference call on October 20, 2026."
    call = find_call(form, date(2026, 10, 1))
    assert call is not None
    assert (call.kind, call.day) == ("Conference call", date(2026, 10, 20))
