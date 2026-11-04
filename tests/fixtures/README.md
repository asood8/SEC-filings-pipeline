# Test fixtures

Real EDGAR documents saved for parser tests, so unit tests never call SEC. Files here are stored
byte-exact (see `.gitattributes`) and named by accession number. Each came from
`https://www.sec.gov/Archives/edgar/data/<cik>/<accession number without dashes>/`.

- `form4/`: Form 4 `ownershipDocument` XML, chosen to cover open-market sales and purchases,
  option exercises, RSU grants, conversions, gifts, tax withholding, joint filers, insiders of
  another issuer, footnote-only prices and dates, remarks, and both `0/1` and `true/false`
  booleans. `services/parser/tests/test_form4.py` says which file covers what.
- `form8k/`: 8-K inline-XBRL HTML covering earnings (2.02), agreements and debt (1.01, 1.02,
  2.03), officer changes and shareholder votes (5.02, 5.07), restructuring (2.05), unregistered
  sales (3.02), Regulation FD disclosures (7.01) that announce investor calls or an Investor Day
  (one filed after its event), and two filing-agent layouts with a table of contents.
  `services/parser/tests/test_form8k.py` lists SEC's item numbers for each.
- `form8k/exhibits/<accession>/`: EX-99 exhibits (press releases) of some `form8k/` filings.
- `form4/amendments/`: Form 4/As filed by or about watchlist companies in the year to 2026-11
  (corrected figures, restated and added lines, a power of attorney only, one amending a 2023
  filing), with the Form 4s they amend and other Form 4s filed the same day.
- `form8k/amendments/`: 8-K/As, six naming the 8-K they amend by its filing date and one that
  doesn't. `services/parser/tests/test_amendments.py` says what each fixture covers.
- `index_headers/`: an EDGAR `-index-headers.html` page, which lists each document of a filing
  (type, file name, description); discovery uses it to find EX-99 exhibits.
