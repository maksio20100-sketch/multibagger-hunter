"""Tests for the financial calculations. They use small made-up SEC data, so
they run instantly and need no internet.

Run them with:   python -m pytest
"""

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import financials as fin  # noqa: E402
import sec_client  # noqa: E402


def entry(start, end, val, form="10-K", filed="2024-02-01", accn="0000000001-24-000001", fy=2023):
    return {"start": start, "end": end, "val": val, "form": form, "filed": filed,
            "accn": accn, "fy": fy, "fp": "FY"}


def company(concepts):
    """Build a fake companyfacts JSON. concepts = {name: (unit, [entries])}."""
    return {
        "cik": 123456,
        "entityName": "Test Corp",
        "facts": {"us-gaap": {name: {"units": {unit: entries}} for name, (unit, entries) in concepts.items()}},
    }


def row_for(result, year_end):
    table = result.table
    return table[table["period_end"] == date.fromisoformat(year_end)].iloc[0]


# --- picking only annual numbers --------------------------------------------
def test_quarterly_and_year_to_date_numbers_are_ignored():
    data = company({"Revenues": ("USD", [
        entry("2023-01-01", "2023-12-31", 1000),                       # full year: keep
        entry("2023-10-01", "2023-12-31", 300),                        # Q4 inside a 10-K: ignore
        entry("2023-01-01", "2023-09-30", 700, form="10-Q"),           # 9-month YTD: ignore
        entry("2023-07-01", "2023-09-30", 250, form="10-Q"),           # quarter: ignore
        entry("2021-01-01", "2023-12-31", 2900),                       # 3-year total: ignore
    ])})
    facts = fin.collect_annual_facts(data, "revenue")
    assert list(facts) == [date(2023, 12, 31)]
    assert facts[date(2023, 12, 31)].value == 1000


def test_52_53_week_fiscal_years_are_accepted():
    data = company({"Revenues": ("USD", [entry("2022-09-25", "2023-09-30", 371)])})  # 371 days
    assert len(fin.collect_annual_facts(data, "revenue")) == 1


def test_non_annual_forms_are_ignored_but_amendments_are_kept():
    data = company({"Revenues": ("USD", [
        entry("2022-01-01", "2022-12-31", 5, form="8-K"),
        entry("2023-01-01", "2023-12-31", 9, form="10-K/A"),
    ])})
    facts = fin.collect_annual_facts(data, "revenue")
    assert list(facts) == [date(2023, 12, 31)]


def test_restated_value_uses_most_recent_filing_and_keeps_its_accession():
    data = company({"Revenues": ("USD", [
        entry("2022-01-01", "2022-12-31", 100, filed="2023-02-01", accn="0000000001-23-000001"),
        entry("2022-01-01", "2022-12-31", 110, filed="2024-02-01", accn="0000000001-24-000009"),  # restated
    ])})
    fact = fin.collect_annual_facts(data, "revenue")[date(2022, 12, 31)]
    assert fact.value == 110 and fact.accn == "0000000001-24-000009"


def test_concept_priority_and_fallback_per_year():
    data = company({
        "RevenueFromContractWithCustomerExcludingAssessedTax": ("USD", [entry("2023-01-01", "2023-12-31", 200)]),
        "Revenues": ("USD", [entry("2023-01-01", "2023-12-31", 999),     # lower priority: ignored
                             entry("2022-01-01", "2022-12-31", 150)]),   # only source for 2022
    })
    facts = fin.collect_annual_facts(data, "revenue")
    assert facts[date(2023, 12, 31)].value == 200
    assert facts[date(2022, 12, 31)].value == 150
    assert facts[date(2022, 12, 31)].concept == "Revenues"


# --- calculations -----------------------------------------------------------
def full_company():
    return company({
        "Revenues": ("USD", [entry("2022-01-01", "2022-12-31", 1000), entry("2023-01-01", "2023-12-31", 1250)]),
        "OperatingIncomeLoss": ("USD", [entry("2022-01-01", "2022-12-31", 100), entry("2023-01-01", "2023-12-31", 250)]),
        "NetCashProvidedByUsedInOperatingActivities": ("USD", [entry("2023-01-01", "2023-12-31", 300)]),
        "PaymentsToAcquirePropertyPlantAndEquipment": ("USD", [entry("2023-01-01", "2023-12-31", 80)]),
        "WeightedAverageNumberOfDilutedSharesOutstanding": ("shares", [entry("2023-01-01", "2023-12-31", 50)]),
    })


def test_growth_margin_and_free_cash_flow_values():
    result = fin.build_annual_result(full_company())
    y23 = row_for(result, "2023-12-31")
    assert y23["revenue_growth"] == pytest.approx(0.25)
    assert y23["operating_margin"] == pytest.approx(0.20)
    assert y23["free_cash_flow"] == pytest.approx(220)  # 300 - 80
    assert pd.isna(row_for(result, "2022-12-31")["revenue_growth"])  # no prior year


def test_missing_input_leaves_result_empty_instead_of_guessing():
    data = full_company()
    del data["facts"]["us-gaap"]["PaymentsToAcquirePropertyPlantAndEquipment"]
    result = fin.build_annual_result(data)
    y23 = row_for(result, "2023-12-31")
    assert pd.isna(y23["free_cash_flow"])
    assert pd.isna(y23["capex"])
    assert any("capital expenditures" in w for w in result.warnings)


def test_growth_is_not_calculated_across_a_missing_year():
    data = company({"Revenues": ("USD", [entry("2020-01-01", "2020-12-31", 100), entry("2022-01-01", "2022-12-31", 150)])})
    result = fin.build_annual_result(data)
    assert pd.isna(row_for(result, "2022-12-31")["revenue_growth"])


def test_derived_values_require_identical_periods():
    # Operating income covers a different (offset) year than revenue.
    data = company({
        "Revenues": ("USD", [entry("2023-01-01", "2023-12-31", 1000)]),
        "OperatingIncomeLoss": ("USD", [entry("2023-01-08", "2023-12-31", 100)]),  # starts a week later: 358 days, still "annual"
    })
    facts_rev = fin.collect_annual_facts(data, "revenue")[date(2023, 12, 31)]
    facts_op = fin.collect_annual_facts(data, "operating_income")[date(2023, 12, 31)]
    assert fin.calc_operating_margin(facts_op, facts_rev) is None


def test_negative_capex_is_not_used():
    data = full_company()
    data["facts"]["us-gaap"]["PaymentsToAcquirePropertyPlantAndEquipment"]["units"]["USD"][0]["val"] = -5
    y23 = row_for(fin.build_annual_result(data), "2023-12-31")
    assert pd.isna(y23["free_cash_flow"])
    assert "negative" in y23["notes"]


SHARES = "WeightedAverageNumberOfDilutedSharesOutstanding"


def test_split_is_detected_from_restated_share_counts_and_old_years_adjusted():
    # NVIDIA-like: FY2021/FY2022 are only reported before the split. FY2023 was
    # reported before (2,500) and again after (25,000) a 10-for-1 split.
    data = company({SHARES: ("shares", [
        entry("2020-02-01", "2021-01-31", 2500, filed="2022-02-01", accn="A-22"),
        entry("2021-02-01", "2022-01-31", 2510, filed="2023-02-01", accn="A-23"),
        entry("2022-02-01", "2023-01-31", 2500, filed="2023-02-01", accn="A-23"),
        entry("2022-02-01", "2023-01-31", 25000, filed="2025-02-01", accn="A-25"),   # restated x10
        entry("2023-02-01", "2024-01-31", 25100, filed="2025-02-01", accn="A-25"),
    ])})
    result = fin.build_annual_result(data)
    assert [s.ratio for s in result.splits] == [10]
    assert any("10-for-1" in n for n in result.notices)
    t = result.table.set_index("period_end")
    # Reported numbers stay exactly as filed...
    assert t.loc[date(2021, 1, 31), "diluted_shares"] == 2500
    # ...and the adjusted column puts older years on today's basis.
    assert t.loc[date(2021, 1, 31), "diluted_shares_adjusted"] == 25000
    assert t.loc[date(2023, 1, 31), "diluted_shares_adjusted"] == 25000  # already restated: not multiplied twice
    assert t.loc[date(2024, 1, 31), "diluted_shares_adjusted"] == 25100
    assert "multiplied by 10" in t.loc[date(2021, 1, 31), "notes"]
    assert not any("even after split adjustment" in w for w in result.warnings)


def test_unrestated_jump_is_not_called_a_split():
    # Regression (Vertiv): shares jumped 118M -> 307M because of a SPAC merger.
    # Nothing was restated by a clean ratio, so no split may be claimed.
    data = company({SHARES: ("shares", [
        entry("2019-01-01", "2019-12-31", 118_261_955, filed="2021-03-01", accn="V-21"),
        entry("2020-01-01", "2020-12-31", 307_076_397, filed="2021-03-01", accn="V-21"),
    ])})
    result = fin.build_annual_result(data)
    assert result.splits == [] and result.notices == []
    jump = [w for w in result.warnings if "even after split adjustment" in w]
    assert jump and "not necessarily a split" in jump[0]
    assert "probably a stock split" not in " ".join(result.warnings)
    assert (result.table["diluted_shares"] == result.table["diluted_shares_adjusted"]).all()


def test_restatement_that_is_not_a_clean_ratio_is_not_a_split():
    data = company({SHARES: ("shares", [
        entry("2022-01-01", "2022-12-31", 1000, filed="2023-02-01", accn="X-23"),
        entry("2022-01-01", "2022-12-31", 1130, filed="2024-02-01", accn="X-24"),  # 1.13x
    ])})
    assert fin.build_annual_result(data).splits == []


def test_zero_revenue_shell_year_is_flagged_and_gets_no_ratios():
    # Regression (Vertiv FY2017): SPAC shell reported revenue of exactly 0.
    data = company({
        "Revenues": ("USD", [entry("2017-01-01", "2017-12-31", 0), entry("2018-01-01", "2018-12-31", 500)]),
        "OperatingIncomeLoss": ("USD", [entry("2017-01-01", "2017-12-31", -3), entry("2018-01-01", "2018-12-31", 50)]),
    })
    result = fin.build_annual_result(data)
    y17, y18 = row_for(result, "2017-12-31"), row_for(result, "2018-12-31")
    assert any("exactly 0" in w for w in result.warnings)
    assert "revenue reported as 0" in y17["notes"]
    assert pd.isna(y17["operating_margin"])    # no division by zero
    assert pd.isna(y18["revenue_growth"])      # no growth from a zero base
    assert y18["operating_margin"] == pytest.approx(0.1)


def test_pre_revenue_shell_row_with_only_cash_flow_is_flagged():
    # Regression (Vertiv FY2016): the SPAC shell had a cash-flow number but no revenue at all.
    data = company({
        "NetCashProvidedByUsedInOperatingActivities": ("USD", [entry("2016-01-01", "2016-12-31", -25000),
                                                               entry("2017-01-01", "2017-12-31", -1276),
                                                               entry("2018-01-01", "2018-12-31", -9000)]),
        "Revenues": ("USD", [entry("2018-01-01", "2018-12-31", 4000)]),
    })
    result = fin.build_annual_result(data)
    assert "no operating revenue" in row_for(result, "2016-12-31")["notes"]
    assert "no operating revenue" in row_for(result, "2017-12-31")["notes"]
    assert "no operating revenue" not in row_for(result, "2018-12-31")["notes"]
    assert any("FY ending 2016-12-31, 2017-12-31" in w for w in result.warnings)


def test_missing_values_are_explained_in_notes():
    # Regression (NVIDIA FY2021 capex, Alphabet pre-2022 shares): blank cells must say why.
    data = full_company()
    del data["facts"]["us-gaap"]["PaymentsToAcquirePropertyPlantAndEquipment"]
    note = row_for(fin.build_annual_result(data), "2023-12-31")["notes"]
    assert "not in SEC data: capital expenditures" in note


def test_restated_year_keeps_first_reported_value_for_comparison():
    data = company({"Revenues": ("USD", [
        entry("2022-01-01", "2022-12-31", 100, filed="2023-02-01", accn="0000000001-23-000001"),
        entry("2022-01-01", "2022-12-31", 110, filed="2024-02-01", accn="0000000001-24-000009"),
    ])})
    fact = fin.collect_annual_facts(data, "revenue")[date(2022, 12, 31)]
    assert (fact.value, fact.first_value) == (110, 100)
    assert fact.first_accn == "0000000001-23-000001"
    src = fin.build_annual_result(data).sources.iloc[0]
    assert src["first_reported_value"] == 100 and src["value"] == 110


def test_fiscal_year_label_comes_from_the_first_filing():
    data = company({"Revenues": ("USD", [
        entry("2022-02-01", "2023-01-31", 100, filed="2023-03-01", accn="A", fy=2023),
        entry("2022-02-01", "2023-01-31", 100, filed="2025-03-01", accn="B", fy=2025),  # later comparison column
    ])})
    assert row_for(fin.build_annual_result(data), "2023-01-31")["fiscal_year"] == 2023


def test_fiscal_year_label_survives_a_change_of_sec_concept_name():
    # Regression: Apple's FY2017 was first filed in 2017 under SalesRevenueNet, and appears
    # under the newer RevenueFromContract... name only in later filings. The label and the
    # "first filed" date must come from the ORIGINAL 2017 filing, not from the later one.
    data = company({
        "SalesRevenueNet": ("USD", [entry("2016-10-01", "2017-09-30", 215, filed="2017-11-03", accn="OLD", fy=2017)]),
        "RevenueFromContractWithCustomerExcludingAssessedTax": ("USD", [
            entry("2016-10-01", "2017-09-30", 229, filed="2019-10-31", accn="NEW", fy=2019)]),
    })
    row = row_for(fin.build_annual_result(data), "2017-09-30")
    assert row["revenue"] == 229                       # newer concept still has priority for the value
    assert row["fiscal_year"] == 2017
    assert row["first_filed"] == date(2017, 11, 3)


def test_comparative_only_year_gets_no_fiscal_year_label():
    # Regression (Vertiv FY2016/17 shell years): first seen two years after the period ended.
    data = company({"Revenues": ("USD", [entry("2016-01-01", "2016-12-31", 5, filed="2019-03-13", fy=2018)])})
    row = row_for(fin.build_annual_result(data), "2016-12-31")
    assert pd.isna(row["fiscal_year"]) and "comparative in a later filing" in row["notes"]


def test_different_fiscal_calendars_and_53_week_years_still_give_growth():
    # NVIDIA-style January year ends (364 and 371 days) and Apple-style September ends.
    data = company({"Revenues": ("USD", [
        entry("2021-02-01", "2022-01-30", 100),       # 363 days
        entry("2022-01-31", "2023-01-29", 150),       # 363 days
        entry("2023-01-30", "2024-01-28", 300),
        entry("2020-02-03", "2021-01-31", 80),        # 53-week year (363 days to Jan 31)
    ])})
    result = fin.build_annual_result(data)
    assert row_for(result, "2023-01-29")["revenue_growth"] == pytest.approx(0.5)
    assert row_for(result, "2024-01-28")["revenue_growth"] == pytest.approx(1.0)
    assert not any("overlap" in w or "apart" in w for w in result.warnings)


def test_overlapping_and_too_close_periods_are_flagged():
    data = company({"Revenues": ("USD", [
        entry("2022-01-01", "2022-12-31", 100),
        entry("2022-07-01", "2023-06-30", 120),   # overlaps the previous year and ends only 181 days later
    ])})
    warnings = " ".join(fin.build_annual_result(data).warnings)
    assert "overlap" in warnings


def test_duplicate_filings_of_the_same_year_do_not_create_extra_rows():
    data = company({"Revenues": ("USD", [
        entry("2023-01-01", "2023-12-31", 9, form="10-K", filed="2024-02-01", accn="A"),
        entry("2023-01-01", "2023-12-31", 9, form="10-K/A", filed="2024-04-01", accn="B"),
        entry("2023-01-01", "2023-12-31", 9, form="10-K", filed="2024-02-01", accn="A"),   # exact duplicate
    ])})
    result = fin.build_annual_result(data)
    assert len(result.table) == 1 and len(fin.annual_candidates(data, "revenue")[date(2023, 12, 31)]) == 2


def test_empty_company_gives_empty_table_and_warning():
    result = fin.build_annual_result(company({}))
    assert result.table.empty and result.warnings


# --- sources -----------------------------------------------------------------
def test_sources_list_accession_numbers_and_links():
    result = fin.build_annual_result(full_company())
    src = result.sources
    assert set(src["accession_number"]) == {"0000000001-24-000001"}
    assert (src["filing_url"] == "https://www.sec.gov/Archives/edgar/data/123456/000000000124000001/0000000001-24-000001-index.htm").all()
    assert {"Revenue", "Operating income", "Operating cash flow", "Capital expenditures",
            "Diluted weighted-average shares"} == set(src["metric"])


# --- SEC client (no network) ------------------------------------------------
def test_user_agent_must_include_contact_email(monkeypatch):
    monkeypatch.setattr(sec_client, "_read_configured_user_agent", lambda: None)
    with pytest.raises(sec_client.MissingUserAgentError):
        sec_client.get_user_agent()
    with pytest.raises(sec_client.MissingUserAgentError):
        sec_client.get_user_agent("JustAName")
    assert sec_client.get_user_agent("App me@example.org") == "App me@example.org"


def test_ticker_map_parsing(monkeypatch):
    fake = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
            "1": {"cik_str": 1652044, "ticker": "googl", "title": "Alphabet Inc."}}
    monkeypatch.setattr(sec_client, "fetch_json", lambda url, ua=None: fake)
    mapping = sec_client.fetch_ticker_map()
    assert mapping["AAPL"]["cik"] == 320193 and mapping["GOOGL"]["cik"] == 1652044
