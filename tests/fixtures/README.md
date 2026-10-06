# Test fixtures

Real EDGAR documents saved for parser tests, so unit tests never call SEC. Files here are stored
byte-exact (see `.gitattributes`) and named by accession number. Each came from
`https://www.sec.gov/Archives/edgar/data/<cik>/<accession number without dashes>/`.

- `form4/`: Form 4 `ownershipDocument` XML, chosen to cover open-market sales and purchases,
  option exercises, RSU grants, conversions, gifts, tax withholding, joint filers, insiders of
  another issuer, footnote-only prices and dates, remarks, and both `0/1` and `true/false`
  booleans. `services/parser/tests/test_form4.py` says which file covers what.
