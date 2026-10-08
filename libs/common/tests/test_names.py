"""Display names. The examples are real EDGAR names (public SEC data): reporting owners from the
local database's form4_filings and issuers from its companies, unless marked otherwise."""

import pytest

from filings_common.names import company_name, display_name


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
