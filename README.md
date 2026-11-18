# Multibagger Hunter

A Python stock scanner with a Streamlit website. The goal is to let users pick
their own investment strategies, including finding potentially undervalued
stocks with 3–5× upside.

**Status:** building the financial-data foundation. The site currently shows
real annual financials from SEC EDGAR for five companies (AAPL, MSFT, NVDA,
VRT, GOOGL). The scanner and strategies are not built yet, and nothing here is
investment advice.

## What the app does today

Pick a company and see, for each fiscal year (from 10-K filings):

- revenue, operating income, operating cash flow, capital expenditures and
  diluted weighted-average shares, as reported;
- calculated revenue growth, operating margin and free cash flow;
- diluted shares both **as reported** and **split-adjusted** (splits are only recognised from evidence in the SEC data itself);
- tables and charts with period dates, units and filing dates;
- a **Sources & verification** tab listing the SEC filing and accession number
  behind every reported number.

Data comes from the SEC's free public APIs (`company_tickers.json` and
`companyfacts`). Only full-year figures are used; quarterly and year-to-date
numbers are excluded, and missing values stay blank rather than being estimated.
Results are cached for 24 hours so the SEC is not asked repeatedly.

## Setup (local)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

The SEC requires every request to say who is asking. Provide a contact in a
git-ignored secrets file (never commit it):

```bash
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# then edit .streamlit/secrets.toml and put your own contact email in
```

Run the app and the tests:

```bash
streamlit run app.py
```

```bash
pip install -r requirements-dev.txt
python -m pytest
```

## Verifying the numbers against the original filings

`scripts/verify_filings.py` does not trust the SEC JSON API. It downloads the actual 10-K / 10-Q documents from
sec.gov and checks that every number the app shows is printed next to the right line label and in the right year
column, that each filing's form and filing date match, and that growth / margin / free cash flow arithmetic is right.

```bash
python scripts/verify_filings.py            # uses the local cache in .sec_cache/ (git-ignored)
python scripts/verify_filings.py --refresh  # re-download everything (polite: ~6 requests/second max)
```

It writes `reports/verification_report.md` and `reports/verification_details.csv` (both git-ignored).

## Deploy on Streamlit Community Cloud

1. Push this repository to GitHub.
2. At https://share.streamlit.io create/open the app with main file `app.py`.
3. In the app's **Settings → Secrets** (private; never in the repo), add:

   ```toml
   SEC_USER_AGENT = "MultibaggerHunter yourname@example.com"
   ```

## Files

- `app.py` — the Streamlit website (dropdown, tables, charts)
- `sec_client.py` — downloads from the SEC with a User-Agent, rate limiting and retries
- `financials.py` — picks annual figures out of the SEC data and does the calculations
- `scripts/verify_filings.py` — audits the app's numbers against the original SEC filings
- `tests/` — offline tests for the calculations (`python -m pytest`)
- `requirements.txt` — packages the app needs; `requirements-dev.txt` adds pytest
- `.streamlit/secrets.toml.example` — template for the (private) SEC contact setting

## Known limitations

- Only five companies so far.
- Capex means cash paid for property, plant and equipment (no finance leases or acquisitions).
- Split adjustment only covers splits the SEC data itself reveals; a jump with no such evidence (merger, SPAC, issuance) is flagged, never adjusted.
- Some values are printed in the filing but missing from the SEC data API (e.g. NVIDIA capex before FY2022, Alphabet consolidated diluted shares before 2022); they stay blank and the Notes column says so.
- SEC concept names vary by company; an unlisted concept shows up as missing data.
