"""Multibagger Hunter - Streamlit website.

Streamlit runs this file from top to bottom every time the page loads or you
change a widget (like the company dropdown). Each `st.something(...)` call
adds an element to the page.

The work is split across three files so each one stays small:
  * sec_client.py - downloads data from the SEC
  * financials.py - cleans that data and does the calculations
  * app.py        - this file: the web page (dropdown, tables, charts)
"""

from datetime import datetime, timezone

import pandas as pd
import streamlit as st

import financials
import sec_client

# The five companies for this first version: {ticker: friendly name}.
COMPANIES = {
    "AAPL": "Apple",
    "MSFT": "Microsoft",
    "NVDA": "NVIDIA",
    "VRT": "Vertiv",
    "GOOGL": "Alphabet",
}

CACHE_SECONDS = 24 * 60 * 60  # SEC annual data changes about once a year


st.set_page_config(page_title="Multibagger Hunter", page_icon="🎯", layout="wide")


# ---------------------------------------------------------------------------
# Cached data loading.
# @st.cache_data remembers a function's result, keyed by its arguments. Opening
# the same company again (even from another visitor) reuses the stored result
# for 24 hours instead of asking the SEC again. Arguments that start with an
# underscore are left out of the cache key (we don't want the User-Agent in it).
# Errors are not cached, so a failed download is retried next time.
# ---------------------------------------------------------------------------
@st.cache_data(ttl=CACHE_SECONDS, show_spinner="Loading SEC ticker list...")
def load_ticker_map(_user_agent: str) -> dict:
    return sec_client.fetch_ticker_map(_user_agent)


@st.cache_data(ttl=CACHE_SECONDS, max_entries=20, show_spinner="Downloading filings data from the SEC...")
def load_annual_result(cik: int, _user_agent: str):
    """Download one company's facts and reduce them to our annual tables.

    We cache the small finished tables, not the multi-megabyte raw JSON.
    """
    raw = sec_client.fetch_company_facts(cik, _user_agent)
    result = financials.build_annual_result(raw)
    return result, datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------
def summary_for_display(table: pd.DataFrame) -> pd.DataFrame:
    """Rename columns, show money in $ millions, newest year first."""
    t = table.copy()
    out = pd.DataFrame({
        "Fiscal year": t["fiscal_year"],
        "Period end": t["period_end"].astype(str),
        "Period start": t["period_start"].astype(str),
        "Days": t["days"],
        "Revenue ($M)": (t["revenue"] / 1e6).round(0),
        "Revenue growth": t["revenue_growth"],
        "Operating income ($M)": (t["operating_income"] / 1e6).round(0),
        "Operating margin": t["operating_margin"],
        "Operating cash flow ($M)": (t["operating_cash_flow"] / 1e6).round(0),
        "Capex ($M)": (t["capex"] / 1e6).round(0),
        "Free cash flow ($M)": (t["free_cash_flow"] / 1e6).round(0),
        "Diluted shares, as reported (M)": (t["diluted_shares"] / 1e6).round(1),
        "Diluted shares, split-adjusted (M)": (t["diluted_shares_adjusted"] / 1e6).round(1),
        "First filed": t["first_filed"].astype(str),
        "Latest filing used": t["filed"].astype(str),
        "Notes": t["notes"],
    })
    return out.iloc[::-1].reset_index(drop=True)


def chart_frame(table: pd.DataFrame, columns: dict, scale: float) -> pd.DataFrame:
    """Build a chart-ready table: one row per fiscal year end, oldest first."""
    frame = pd.DataFrame({label: table[col] * scale for label, col in columns.items()})
    frame.index = table["period_end"].astype(str)
    return frame


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------
st.title("Multibagger Hunter")
st.subheader("Multibagger Hunter — Stock Discovery Engine")
st.caption("Step 1: the financial-data foundation, using free public SEC EDGAR data. "
           "Scanner and screening strategies are not built yet.")

# 1. Who are we, as far as the SEC is concerned?
try:
    user_agent = sec_client.get_user_agent()
except sec_client.MissingUserAgentError:
    st.error("The SEC requires every request to identify who is asking. "
             "No contact details are configured yet, so the app cannot fetch data.")
    st.markdown(
        "Set **`SEC_USER_AGENT`** to something like `MultibaggerHunter yourname@example.com`:\n"
        "- **Locally:** copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and edit it "
        "(that file is git-ignored, so it never reaches GitHub).\n"
        "- **Streamlit Community Cloud:** app **Settings → Secrets**, paste the same line."
    )
    st.stop()

# 2. Pick a company.
ticker = st.selectbox(
    "Company",
    options=list(COMPANIES),
    format_func=lambda t: f"{t} — {COMPANIES[t]}",
)

# 3. Look up its SEC ID (CIK) and load the data.
try:
    ticker_map = load_ticker_map(user_agent)
    if ticker not in ticker_map:
        st.error(f"{ticker} was not found in the SEC ticker list.")
        st.stop()
    cik = ticker_map[ticker]["cik"]
    result, retrieved_at = load_annual_result(cik, user_agent)
except sec_client.SecError as exc:
    st.error(f"Could not load data from the SEC: {exc}")
    st.stop()

st.markdown(f"### {result.company_name} ({ticker})")
st.caption(
    f"SEC CIK {cik:010d} · data retrieved {retrieved_at:%Y-%m-%d %H:%M} UTC "
    f"(cached up to 24 hours) · "
    f"[SEC filing list](https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik:010d}&type=10-K) · "
    f"[raw SEC data]({sec_client.COMPANYFACTS_URL.format(cik=f'{cik:010d}')})"
)

for message in result.notices:
    st.info(message)
for message in result.warnings:
    st.warning(message)

if result.table.empty:
    st.stop()

tab_summary, tab_charts, tab_sources, tab_method = st.tabs(
    ["Annual summary", "Charts", "Sources & verification", "How this works"]
)

# --- Tab 1: the table -------------------------------------------------------
with tab_summary:
    st.markdown("Annual figures from 10-K filings. Money is in **US$ millions**; shares in millions. "
                "A blank cell means the value was not reported or could not be calculated honestly.")
    st.dataframe(
        summary_for_display(result.table),
        hide_index=True,
        width="stretch",
        column_config={
            "Days": st.column_config.NumberColumn(format="%d", help="Length of the reporting period"),
            "Revenue ($M)": st.column_config.NumberColumn(format="localized"),
            "Revenue growth": st.column_config.NumberColumn(format="percent"),
            "Operating income ($M)": st.column_config.NumberColumn(format="localized"),
            "Operating margin": st.column_config.NumberColumn(format="percent"),
            "Operating cash flow ($M)": st.column_config.NumberColumn(format="localized"),
            "Capex ($M)": st.column_config.NumberColumn(format="localized"),
            "Free cash flow ($M)": st.column_config.NumberColumn(format="localized"),
            "Diluted shares, as reported (M)": st.column_config.NumberColumn(
                format="localized", help="Exactly as printed in the cited filing. Not comparable across a stock split."),
            "Diluted shares, split-adjusted (M)": st.column_config.NumberColumn(
                format="localized", help="Reported shares multiplied by the ratio of any later split that the SEC data "
                                         "itself shows. Derived, not reported."),
            "Fiscal year": st.column_config.NumberColumn(format="%d", help="The company's own fiscal-year label"),
            "First filed": st.column_config.TextColumn(help="Filing date of the first 10-K that reported this year"),
        },
    )
    st.caption("Revenue growth = revenue ÷ prior-year revenue − 1.  Operating margin = operating income ÷ revenue.  "
               "Free cash flow = operating cash flow − capital expenditures.")

# --- Tab 2: charts ----------------------------------------------------------
with tab_charts:
    table = result.table
    st.markdown("**Revenue** (US$ billions)")
    st.bar_chart(chart_frame(table, {"Revenue": "revenue"}, 1e-9), y_label="US$ billions", x_label="Fiscal year end")

    st.markdown("**Revenue growth and operating margin** (%)")
    st.line_chart(
        chart_frame(table, {"Revenue growth": "revenue_growth", "Operating margin": "operating_margin"}, 100),
        y_label="percent", x_label="Fiscal year end",
    )

    st.markdown("**Operating cash flow, capital expenditures and free cash flow** (US$ billions)")
    st.bar_chart(
        chart_frame(table, {"Operating cash flow": "operating_cash_flow", "Capex": "capex",
                            "Free cash flow": "free_cash_flow"}, 1e-9),
        y_label="US$ billions", x_label="Fiscal year end", stack=False,
    )

    st.markdown("**Diluted weighted-average shares** (millions): as reported vs split-adjusted")
    st.line_chart(
        chart_frame(table, {"As reported": "diluted_shares", "Split-adjusted": "diluted_shares_adjusted"}, 1e-6),
        y_label="millions of shares", x_label="Fiscal year end",
    )
    st.caption("'As reported' is exactly what the cited filing prints, so it jumps at stock splits. "
               "'Split-adjusted' restates older years using only splits the SEC data itself reveals "
               "(a later filing restating the same year by a clean ratio). The lines overlap when no split applies.")

# --- Tab 3: where each number came from ------------------------------------
with tab_sources:
    st.markdown("Every reported value, with the SEC filing it came from. Open the filing and look for the "
                "same number in the financial statements to verify it. Values here are in **full units** "
                "(US dollars or number of shares), exactly as reported.")
    st.dataframe(
        result.sources.assign(period_start=lambda d: d["period_start"].astype(str),
                              period_end=lambda d: d["period_end"].astype(str),
                              filed=lambda d: d["filed"].astype(str)),
        hide_index=True,
        width="stretch",
        column_config={
            "metric": "Metric",
            "period_start": "Period start",
            "period_end": "Period end",
            "value": st.column_config.NumberColumn("Value", format="localized"),
            "unit": "Unit",
            "sec_concept": "SEC concept",
            "form": "Form",
            "filed": "Filed",
            "accession_number": "Accession number",
            "filing_url": st.column_config.LinkColumn("Filing", display_text="Open on sec.gov"),
            "first_filed": "First filed",
            "first_reported_value": st.column_config.NumberColumn("Value in first filing", format="localized"),
            "first_accession_number": "First filing accession",
        },
    )

# --- Tab 4: method and limits ----------------------------------------------
with tab_method:
    st.markdown("""
**Data source:** the SEC's free XBRL "company facts" API (`data.sec.gov`) and its company ticker list.

**How periods are kept clean**
- Only facts from annual reports (10-K, 10-K/A) are used.
- Only facts covering **350-380 days** are used. Quarterly, half-year and nine-month (year-to-date)
  figures are measured by their actual dates and excluded.
- If a later filing restated a year, the most recently filed value is used and its accession number is shown;
  the value from the first filing is kept alongside it (Sources tab) so restatements are visible.
- Fiscal years are labelled with the company's own label from the filing that first reported them, next to
  the exact period start/end dates, because companies end their years in different months.
- **Share counts:** "as reported" is exactly what the cited filing prints. "Split-adjusted" is derived: a split is
  only recognised when the SEC data shows the *same fiscal year* restated by a clean ratio (2, 3, 4, 10, 20...) in a
  later filing. Older years are then multiplied by that ratio. A jump with no such evidence (a merger, SPAC or
  big issuance) is flagged but never adjusted.
- Growth, margin and free cash flow are calculated only when every input covers *exactly* the same period.
  Growth needs the previous fiscal year to be present too.
- Nothing is estimated or filled in. Missing stays blank.

**Known limitations**
- Companies label the same thing with different SEC concept names. We try a fixed list per metric
  (shown in the Sources tab) and use the first one found. A company using an unlisted concept shows as missing.
- *Capital expenditures* means cash paid for property, plant and equipment. It excludes finance-lease
  additions, acquisitions and purchased intangibles, so free cash flow here is a simple definition.
- *Operating income* is as the company reports it (it can include unusual items).
- Fiscal years differ (Apple ends in September, Microsoft in June, NVIDIA in January), so years are
  labelled by period end date, not by calendar year.
- Split adjustment only covers splits the SEC data itself reveals; share counts before that evidence stay as reported.
- Some values exist in the filing but are missing from the SEC's data API (the Notes column says when).
- Companies that recently listed (for example through a SPAC merger) may have a short history.
- Not investment advice.
""")
