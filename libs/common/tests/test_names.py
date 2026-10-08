"""Display names and owners' relationships. The examples are real EDGAR names and officer titles
(public SEC data): reporting owners from the local database's form4_filings and MongoDB payloads,
and issuers from its companies, unless marked otherwise."""

import pytest

from filings_common.names import (
    OFFICER_ABBREVIATIONS,
    company_name,
    display_name,
    officer_title,
    relationship_label,
)
from filings_common.parsed import OwnerRelationship
from filings_common.search import ABBREVIATIONS


@pytest.mark.parametrize(
    ("name", "shown"),
    [
        # People in capitals are reordered and title-cased.
        ("COOK TIMOTHY D", "Timothy D Cook"),
        ("HUANG JEN HSUN", "Jen Hsun Huang"),
        ("O'BRIEN DEIRDRE", "Deirdre O'Brien"),
        ("CHAVEZ R. MARTIN", "R. Martin Chavez"),
        ("SMITH JOHN A JR", "John A Smith Jr"),  # made up
        # Mixed case is kept as typed, only reordered.
        ("Herrington Douglas J", "Douglas J Herrington"),
        ("McLaughlin Edward Grunde", "Edward Grunde McLaughlin"),
        ("DeVeydt Wayne S", "Wayne S DeVeydt"),
        ("O'Toole Amie Thuener", "Amie Thuener O'Toole"),
        # Suffixes stay last.
        ("Ferguson Roger W. Jr.", "Roger W. Ferguson Jr."),
        ("Huntsman Jon M Jr", "Jon M Huntsman Jr"),
        ("MCNABB FREDERICK WILLIAM III", "Frederick William McNabb III"),
        ("SMITH JOHN JR.", "John Smith Jr."),  # made up
        ("JONES MARY PHD", "Mary Jones PhD"),  # made up
        ("JONES MARY M.D.", "Mary Jones M.D."),  # made up
        # Mc names in capitals.
        ("MCDONALD CALVIN", "Calvin McDonald"),
        ("MCINERNEY RYAN", "Ryan McInerney"),
        # A particle starts a last name of several words.
        ("Di Sibio Carmine", "Carmine Di Sibio"),
        ("VAN DER BERG JOHN", "John Van Der Berg"),  # made up
        ("DE LA CRUZ MARIA", "Maria De La Cruz"),  # made up
        ("DI JOHN", "John Di"),  # made up: with two words, Di is the last name
        ("LE JOHN M", "John M Le"),  # made up: Le alone is a last name, not a particle
        # A comma after the last name.
        ("SMITH, JOHN", "John Smith"),  # made up
        # Entities keep their word order.
        ("BERKSHIRE HATHAWAY INC", "Berkshire Hathaway Inc"),
        ("BANK OF AMERICA CORP /DE/", "Bank of America Corp"),
        ("Banc of America Preferred Funding Corp", "Banc of America Preferred Funding Corp"),
        ("SMITH FAMILY TRUST", "Smith Family Trust"),  # made up
        ("ACME PARTNERS, L.P.", "Acme Partners, L.P."),  # made up
        ("ESTATE OF JOHN SMITH", "Estate of John Smith"),  # made up
        ("JOHNSON & JOHNSON", "Johnson & Johnson"),
        ("ALLIANCE RESOURCE PARTNERS LP", "Alliance Resource Partners LP"),
        # One word.
        ("CHAN", "Chan"),  # made up
        # Known limits (see the module docstring): the API also returns the name as filed.
        ("MONTGOMERY RICE VALERIE MD", "Rice Valerie Montgomery MD"),  # two-word last name
        ("SACHIN J. MEHRA", "J. Mehra Sachin"),  # filed first name first
    ],
)
def test_display_name(name: str, shown: str) -> None:
    assert display_name(name) == shown


@pytest.mark.parametrize(
    ("name", "shown"),
    [
        ("LENNAR CORP /NEW/", "Lennar Corp"),
        ("COSTCO WHOLESALE CORP /NEW", "Costco Wholesale Corp"),
        ("CHEVRON CORP", "Chevron Corp"),
        ("BANK OF AMERICA CORP /DE/", "Bank of America Corp"),
        ("Tesla, Inc.", "Tesla, Inc."),
        ("NIKE, Inc.", "NIKE, Inc."),
        ("NETFLIX INC", "Netflix Inc"),
        ("VISA INC.", "Visa Inc."),
        ("MCDONALDS CORP", "McDonald's Corp"),
        # Capitals except a short word, which keeps its spelling.
        ("ELI LILLY & Co", "Eli Lilly & Co"),
        ("3i GROUP PLC", "3i Group Plc"),
        ("GRUPO SIMEC, S.A.B. de C.V.", "Grupo Simec, S.A.B. de C.V."),
        # Letters around an ampersand stay in capitals.
        ("AT&T INC.", "AT&T Inc."),
        ("H&R BLOCK INC", "H&R Block Inc"),
        ("JOHNSON & JOHNSON", "Johnson & Johnson"),
        ("MARSH & MCLENNAN COMPANIES, INC.", "Marsh & McLennan Companies, Inc."),
        # So do Roman numerals and legal forms written in capitals.
        ("PIMCO MUNICIPAL INCOME FUND II", "Pimco Municipal Income Fund II"),
        ("ALLIANCE RESOURCE PARTNERS LP", "Alliance Resource Partners LP"),
        ("AKZO NOBEL NV", "Akzo Nobel NV"),
        ("3M CO", "3M Co"),
        ("DICK'S SPORTING GOODS, INC.", "Dick's Sporting Goods, Inc."),
        ("MCI INC", "Mci Inc"),  # made up: not a Mc name
        # Brands with their own spelling, and ".com".
        ("JPMORGAN CHASE & CO", "JPMorgan Chase & Co"),
        ("PEPSICO INC", "PepsiCo Inc"),
        ("UNITEDHEALTH GROUP INC", "UnitedHealth Group Inc"),
        ("NVIDIA CORP", "NVIDIA Corp"),
        ("AMAZON COM INC", "Amazon.com Inc"),
        ("SAP SE", "SAP SE"),
        ("BOOKING COM", "Booking.com"),
        ("COM DEV INTERNATIONAL", "Com Dev International"),  # made up: a first word stays one
    ],
)
def test_company_name(name: str, shown: str) -> None:
    assert company_name(name) == shown
    assert company_name(shown) == shown  # idempotent, so callers may pass either


# --- Owners' relationships -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "shown"),
    [
        # The usual long forms are abbreviated, whatever their spacing.
        ("Chief Executive Officer", "CEO"),
        ("Chief Financial Officer", "CFO"),
        ("EVP, Chief  Accounting Officer", "EVP, CAO"),
        ("Senior Vice President,  CFO", "SVP, CFO"),
        ("EVP & Chief Financial Officer", "EVP & CFO"),
        ("Executive Vice President", "EVP"),
        ("Sr. Executive Vice President", "Sr. EVP"),
        ("VP, Chief Accounting Officer", "VP, CAO"),
        ("Chief Executive Officer,  UHC", "CEO, UHC"),
        ("Senior Executive Vice-President", "Senior EVP"),  # made up
        # A title in capitals is title-cased, but for its abbreviations and small words.
        ("GENERAL COUNSEL", "General Counsel"),
        ("EVP: CHIEF OPERATING OFFICER", "EVP: COO"),
        ("CEO, UHG", "CEO, UHG"),
        ("CHIEF OF STAFF", "Chief of Staff"),  # made up
        ("SVP, MKTG AND SALES", "SVP, MKTG and Sales"),  # made up
        # Anything else stays as typed.
        ("SVP, GC and Government Affairs", "SVP, GC and Government Affairs"),
        ("COB and CEO", "COB and CEO"),
        ("Vice Chair and President", "Vice Chair and President"),
        ("Chief Product Officer", "Chief Product Officer"),
        ("President & CTO, MA Tech", "President & CTO, MA Tech"),
        ("Co-Founder", "Co-Founder"),
    ],
)
def test_officer_title(title: str, shown: str) -> None:
    assert officer_title(title) == shown


def test_officer_abbreviations_are_search_synonyms() -> None:
    assert dict(ABBREVIATIONS).items() >= OFFICER_ABBREVIATIONS.items()


def role(**ticked: bool | str) -> OwnerRelationship:
    return OwnerRelationship.model_validate(
        {
            "is_director": False,
            "is_officer": False,
            "is_ten_percent_owner": False,
            "is_other": False,
            "officer_title": None,
            "other_text": None,
        }
        | ticked
    )


@pytest.mark.parametrize(
    ("relationship", "label"),
    [
        (role(is_director=True), "Director"),
        (role(is_officer=True, officer_title="Chief Financial Officer"), "CFO"),
        (role(is_ten_percent_owner=True), "10% owner"),  # Berkshire Hathaway buying Lennar
        # Several boxes: the officer title first, as the Form 4 template writes them.
        (
            role(is_director=True, is_officer=True, officer_title="President and CEO"),
            "President and CEO, Director",
        ),
        (
            role(
                is_director=True,
                is_officer=True,
                is_ten_percent_owner=True,
                officer_title="COB and CEO",
            ),
            "COB and CEO, Director, 10% owner",
        ),
        # An officer without a title, and other relationships (made up).
        (role(is_officer=True), "Officer"),
        (role(is_officer=True, officer_title="  "), "Officer"),
        (role(is_other=True, other_text="Member of 10%  owner group"), "Member of 10% owner group"),
        (role(is_other=True), "Other"),
        (role(is_director=True, is_other=True, other_text="See Remarks"), "Director, See Remarks"),
        # A title or text without its box ticked is left out, and no box is no label.
        (role(officer_title="Chief Executive Officer"), None),
        (role(), None),
    ],
)
def test_relationship_label(relationship: OwnerRelationship, label: str | None) -> None:
    assert relationship_label(relationship) == label
