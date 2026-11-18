"""Turn the SEC's raw "company facts" JSON into clean annual financials.

No internet and no Streamlit in this file - it only receives the JSON and
returns tables. That makes it easy to test with small made-up examples.

HOW THE SEC DATA LOOKS
----------------------
The companyfacts JSON is one big dictionary:

    facts -> us-gaap -> <Concept name> -> units -> <unit> -> [list of facts]

Each fact is one reported number, e.g.

    {"start": "2023-10-01", "end": "2024-09-28", "val": 391035000000,
     "accn": "0000320193-24-000123", "form": "10-K", "filed": "2024-11-01", ...}

The same number is repeated every time a later filing shows it as a
comparison figure, and the file also contains quarterly (3-month) and
year-to-date (6/9-month) numbers for the same concepts. So we must be
careful to pick only *full-year* numbers.

OUR RULES
---------
1. Only facts from annual reports (form 10-K or 10-K/A).
2. Only facts that cover a period of 350-380 days (about one year; 52/53-week
   fiscal years such as Apple's are 364 or 371 days). This is what actually
   keeps quarterly and year-to-date figures out - we measure the dates
   instead of trusting labels.
3. If the same fiscal year appears in several filings, show the value from the
   most recently filed one (it includes any restatement) and remember which
   filing it came from, plus the value from the FIRST filing for comparison.
4. Derived numbers (growth, margin, free cash flow) are only calculated when
   every input covers exactly the same period. Otherwise they stay empty.
5. Missing is missing: we never fill in, estimate or interpolate a number.
6. Share counts are shown exactly as reported in the cited filing, AND on a
   split-adjusted basis. Splits are only detected from evidence in the SEC
   data itself (see detect_splits).
"""

from dataclasses import dataclass, replace
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

ANNUAL_FORMS = ("10-K", "10-K/A")
MIN_ANNUAL_DAYS = 350
MAX_ANNUAL_DAYS = 380
# A share count that still jumps by more than this factor in one year after
# split adjustment is flagged (merger, IPO/SPAC, big issuance... or a split
# we have no evidence for).
SHARE_JUMP_RATIO = 1.8
# Ratios that real stock splits come in. A restated share count is only
# treated as a split if it changed by one of these (within 1%).
CLEAN_SPLIT_RATIOS = (
    [1.5, 2, 3, 4, 5, 6, 7, 8, 10, 15, 20, 25, 30, 40, 50]
    + [1 / x for x in (1.5, 2, 3, 4, 5, 6, 7, 8, 10, 15, 20, 25, 30, 40, 50)]
)
SPLIT_TOLERANCE = 0.01


@dataclass(frozen=True)
class MetricSpec:
    """Describes one thing we want to read from the SEC data."""

    label: str
    unit: str  # SEC unit name: "USD" for money, "shares" for share counts
    concepts: Tuple[str, ...]  # us-gaap concept names, best match first


# Companies describe the same idea with different XBRL concept names (and
# change names over time), so each metric has an ordered list. For each
# fiscal year we use the first concept in the list that has a value.
METRICS: Dict[str, MetricSpec] = {
    "revenue": MetricSpec(
        "Revenue",
        "USD",
        (
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
            "SalesRevenueNet",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "SalesRevenueGoodsNet",
        ),
    ),
    "operating_income": MetricSpec("Operating income", "USD", ("OperatingIncomeLoss",)),
    "operating_cash_flow": MetricSpec(
        "Operating cash flow",
        "USD",
        (
            "NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        ),
    ),
    "capex": MetricSpec(
        "Capital expenditures",
        "USD",
        (
            "PaymentsToAcquirePropertyPlantAndEquipment",
            "PaymentsToAcquireProductiveAssets",
            "PaymentsForCapitalImprovements",
        ),
    ),
    "diluted_shares": MetricSpec(
        "Diluted weighted-average shares",
        "shares",
        ("WeightedAverageNumberOfDilutedSharesOutstanding",),
    ),
}

NUMBER_COLUMNS = (
    "revenue", "operating_income", "operating_cash_flow", "capex", "diluted_shares",
    "diluted_shares_adjusted", "revenue_growth", "operating_margin", "free_cash_flow", "fiscal_year",
)


@dataclass(frozen=True)
class Fact:
    """One reported number, with everything needed to look it up at the SEC."""

    metric: str
    concept: str
    unit: str
    start: date
    end: date
    value: float
    form: str
    filed: date
    accn: str  # accession number: the SEC's unique ID for the filing
    fy: Optional[int] = None  # fiscal year label the company gave in that filing
    # The same period as it appeared in the FIRST filing that reported it:
    first_value: Optional[float] = None
    first_filed: Optional[date] = None
    first_accn: Optional[str] = None
    first_fy: Optional[int] = None

    @property
    def days(self) -> int:
        return (self.end - self.start).days


@dataclass(frozen=True)
class SplitEvent:
    """A stock split inferred from the SEC data itself."""

    ratio: float  # 10.0 means 10-for-1; 0.5 would be a 1-for-2 reverse split
    effective_filed: date  # first filing whose numbers are on the new basis
    evidence_period_end: date  # the fiscal year whose restatement revealed it
    old_value: float
    new_value: float
    old_accn: str
    new_accn: str

    def describe(self) -> str:
        ratio = f"{self.ratio:g}-for-1" if self.ratio >= 1 else f"1-for-{1 / self.ratio:g} (reverse)"
        return (f"{ratio} stock split: diluted shares for the year ending {self.evidence_period_end} "
                f"are {self.old_value:,.0f} in filing {self.old_accn} but {self.new_value:,.0f} in the "
                f"later filing {self.new_accn} (filed {self.effective_filed}).")


@dataclass
class AnnualResult:
    company_name: str
    cik: int
    table: pd.DataFrame  # one row per fiscal year
    sources: pd.DataFrame  # one row per reported value, with accession number
    warnings: List[str]  # things that may make numbers misleading
    notices: List[str]  # neutral information (e.g. detected splits)
    splits: List[SplitEvent]


# ---------------------------------------------------------------------------
# Step 1: pick the annual facts out of the raw JSON
# ---------------------------------------------------------------------------
def _parse_date(text: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(text))
    except ValueError:
        return None


def annual_candidates(company_facts: Dict[str, Any], metric: str) -> Dict[date, List[Fact]]:
    """For one metric, return every annual fact for each fiscal-year end.

    {period_end: [Fact, Fact, ...]} - one Fact per filing (accession number),
    oldest filing first. Quarterly / year-to-date / multi-year facts and
    non-annual forms are excluded.
    """
    spec = METRICS[metric]
    us_gaap = company_facts.get("facts", {}).get("us-gaap", {})
    chosen: Dict[date, List[Fact]] = {}

    for concept in spec.concepts:
        entries = us_gaap.get(concept, {}).get("units", {}).get(spec.unit, [])
        per_end: Dict[date, Dict[str, Fact]] = {}

        for entry in entries:
            if entry.get("form") not in ANNUAL_FORMS:
                continue
            start, end = _parse_date(entry.get("start")), _parse_date(entry.get("end"))
            filed = _parse_date(entry.get("filed"))
            value = entry.get("val")
            if start is None or end is None or filed is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if not MIN_ANNUAL_DAYS <= (end - start).days <= MAX_ANNUAL_DAYS:
                continue  # a quarter, a half-year, a multi-year total...

            fy = entry.get("fy")
            fact = Fact(metric, concept, spec.unit, start, end, float(value), entry["form"], filed,
                        str(entry.get("accn", "")), fy if isinstance(fy, int) else None)
            per_end.setdefault(end, {})[fact.accn] = fact

        for end, by_accn in per_end.items():
            # An earlier concept in the priority list wins for this period.
            chosen.setdefault(end, sorted(by_accn.values(), key=lambda f: (f.filed, f.accn)))
    return chosen


def earliest_reports(company_facts: Dict[str, Any]) -> Dict[date, Tuple[date, Optional[int]]]:
    """For each fiscal-year end: (filing date, fiscal-year label) of the FIRST annual report to show it.

    Looks across every concept name we know for every metric. Companies change concept
    names over time (Apple's revenue concept changed in 2018), so the first filing for a
    year may use an older name than the one we display. The fiscal-year label is the
    company's own, taken from that original filing.
    """
    earliest: Dict[date, Tuple[date, Optional[int]]] = {}
    us_gaap = company_facts.get("facts", {}).get("us-gaap", {})
    for spec in METRICS.values():
        for concept in spec.concepts:
            for entry in us_gaap.get(concept, {}).get("units", {}).get(spec.unit, []):
                if entry.get("form") not in ANNUAL_FORMS:
                    continue
                start, end, filed = (_parse_date(entry.get(k)) for k in ("start", "end", "filed"))
                if start is None or end is None or filed is None:
                    continue
                if not MIN_ANNUAL_DAYS <= (end - start).days <= MAX_ANNUAL_DAYS:
                    continue
                fy = entry.get("fy") if isinstance(entry.get("fy"), int) else None
                if end not in earliest or filed < earliest[end][0]:
                    earliest[end] = (filed, fy)
    return earliest


def collect_annual_facts(company_facts: Dict[str, Any], metric: str) -> Dict[date, Fact]:
    """Return {fiscal_year_end_date: Fact} for one metric, annual figures only.

    The Fact is the one from the most recently filed 10-K (restatements
    included); it also records what the FIRST filing reported.
    """
    result: Dict[date, Fact] = {}
    for end, facts in annual_candidates(company_facts, metric).items():
        first, latest = facts[0], facts[-1]
        result[end] = replace(latest, first_value=first.value, first_filed=first.filed,
                              first_accn=first.accn, first_fy=first.fy)
    return result


# ---------------------------------------------------------------------------
# Step 2: stock splits. Reported share counts are only comparable over time
# if we know about splits. We never assume one: we look for a fiscal year
# whose share count was RESTATED by a clean split ratio in a later filing.
# ---------------------------------------------------------------------------
def _clean_ratio(ratio: float) -> Optional[float]:
    for candidate in CLEAN_SPLIT_RATIOS:
        if abs(ratio / candidate - 1) <= SPLIT_TOLERANCE:
            return candidate
    return None


def detect_splits(share_candidates: Dict[date, List[Fact]]) -> List[SplitEvent]:
    """Find splits by comparing the same fiscal year across successive filings."""
    found: Dict[Tuple[float, date], SplitEvent] = {}
    for end, facts in share_candidates.items():
        for older, newer in zip(facts, facts[1:]):
            if older.value <= 0 or newer.value <= 0:
                continue
            clean = _clean_ratio(newer.value / older.value)
            if clean is None:
                continue
            key = (clean, newer.filed)
            found.setdefault(key, SplitEvent(clean, newer.filed, end, older.value, newer.value,
                                             older.accn, newer.accn))
    return sorted(found.values(), key=lambda s: s.effective_filed)


def split_factor(fact: Fact, splits: List[SplitEvent]) -> float:
    """How many of today's shares one reported share is worth.

    A value reported in a filing made BEFORE a split's restated filing is on the
    old basis, so it is multiplied by that split's ratio.
    """
    factor = 1.0
    for split in splits:
        if fact.filed < split.effective_filed:
            factor *= split.ratio
    return factor


# ---------------------------------------------------------------------------
# Step 3: calculations. Each returns None when it can't be done honestly.
# ---------------------------------------------------------------------------
def same_period(*facts: Optional[Fact]) -> bool:
    """True only if all facts exist and cover exactly the same dates."""
    if any(f is None for f in facts):
        return False
    return len({(f.start, f.end) for f in facts}) == 1


def calc_revenue_growth(current: Optional[Fact], previous: Optional[Fact]) -> Optional[float]:
    """Year-over-year change. Needs two consecutive fiscal years of revenue."""
    if current is None or previous is None or previous.value <= 0:
        return None
    gap_days = (current.end - previous.end).days
    if not MIN_ANNUAL_DAYS <= gap_days <= MAX_ANNUAL_DAYS:
        return None  # a missing year in between: not a one-year growth rate
    return current.value / previous.value - 1


def calc_operating_margin(operating_income: Optional[Fact], revenue: Optional[Fact]) -> Optional[float]:
    if not same_period(operating_income, revenue) or revenue.value <= 0:
        return None
    return operating_income.value / revenue.value


def calc_free_cash_flow(operating_cash_flow: Optional[Fact], capex: Optional[Fact]) -> Optional[float]:
    """Free cash flow = operating cash flow - capital expenditures.

    The SEC reports capex as a positive "payment" amount, so we subtract it.
    A negative capex would be unusual (a refund), so we do not guess.
    """
    if not same_period(operating_cash_flow, capex) or capex.value < 0:
        return None
    return operating_cash_flow.value - capex.value


# ---------------------------------------------------------------------------
# Step 4: assemble the tables
# ---------------------------------------------------------------------------
def filing_index_url(cik: int, accn: str) -> str:
    """Link to the filing's page on sec.gov, where the numbers can be verified."""
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn.replace('-', '')}/{accn}-index.htm"


def build_annual_result(company_facts: Dict[str, Any], max_years: int = 10) -> AnnualResult:
    cik = int(company_facts.get("cik", 0))
    name = str(company_facts.get("entityName", "Unknown company"))
    warnings: List[str] = []
    notices: List[str] = []

    share_candidates = annual_candidates(company_facts, "diluted_shares")
    splits = detect_splits(share_candidates)

    facts_by_metric = {m: collect_annual_facts(company_facts, m) for m in METRICS}
    first_reports = earliest_reports(company_facts)
    for metric, found in facts_by_metric.items():
        if not found:
            warnings.append(
                f"No annual {METRICS[metric].label.lower()} was found in the SEC data "
                f"(looked for: {', '.join(METRICS[metric].concepts)})."
            )

    all_ends = sorted({end for found in facts_by_metric.values() for end in found})
    rows = []
    for end in all_ends:
        f = {m: facts_by_metric[m].get(end) for m in METRICS}
        present = [x for x in f.values() if x is not None]
        notes: List[str] = []
        if len({(x.start, x.end) for x in present}) > 1:
            notes.append("inputs cover different periods; derived values skipped where unmatched")

        # Previous fiscal year's revenue = the revenue fact ending ~1 year earlier.
        previous = next(
            (facts_by_metric["revenue"][e] for e in reversed(all_ends)
             if e < end and e in facts_by_metric["revenue"]),
            None,
        )
        capex, shares = f["capex"], f["diluted_shares"]
        if capex is not None and capex.value < 0:
            notes.append("capex reported as negative; free cash flow not calculated")
        if f["revenue"] is not None and f["revenue"].value == 0:
            notes.append("revenue reported as 0 (shell company or pre-merger entity?)")

        missing = [METRICS[m].label.lower() for m in METRICS if f[m] is None]
        if missing:
            notes.append("not in SEC data: " + ", ".join(missing))

        adjusted = None
        if shares is not None:
            factor = split_factor(shares, splits)
            adjusted = shares.value * factor
            if factor != 1:
                notes.append(f"shares multiplied by {factor:g} to adjust for later split(s)")

        first_filed, first_fy = first_reports.get(end, (min(x.first_filed for x in present), None))
        if (first_filed - end).days > 400:
            # Never reported in its own year's 10-K, only as a comparative in a much later filing:
            # the later filing's fiscal-year label would be wrong for this period.
            first_fy = None
            notes.append("first appears in the SEC data only as a comparative in a later filing; fiscal-year label unknown")
        row = {
            "fiscal_year": first_fy,
            "period_end": end,
            "period_start": present[0].start,
            "days": present[0].days,
            "first_filed": first_filed,
            "filed": max(x.filed for x in present),
            "revenue": f["revenue"].value if f["revenue"] else None,
            "operating_income": f["operating_income"].value if f["operating_income"] else None,
            "operating_cash_flow": f["operating_cash_flow"].value if f["operating_cash_flow"] else None,
            "capex": capex.value if capex else None,
            "diluted_shares": shares.value if shares else None,
            "diluted_shares_adjusted": adjusted,
            "revenue_growth": calc_revenue_growth(f["revenue"], previous),
            "operating_margin": calc_operating_margin(f["operating_income"], f["revenue"]),
            "free_cash_flow": calc_free_cash_flow(f["operating_cash_flow"], capex),
            "notes": notes,
        }
        rows.append(row)

    table = pd.DataFrame(rows)
    if table.empty:
        warnings.append("No annual (10-K) data could be extracted for this company.")
        return AnnualResult(name, cik, table, pd.DataFrame(), warnings, notices, splits)

    # Make every number column numeric so "missing" is always NaN (never None).
    for column in NUMBER_COLUMNS:
        table[column] = pd.to_numeric(table[column], errors="coerce")

    table = table.tail(max_years).reset_index(drop=True)  # only what we display gets checked
    # Only mention splits that happened after the oldest year we display (older ones affect nothing shown).
    notices.extend(sp.describe() for sp in splits if sp.effective_filed > table["first_filed"].min())
    _flag_share_count_jumps(table, warnings)
    _flag_zero_revenue(table, warnings)
    _check_period_integrity(table, warnings)
    _flag_stale_data(table, warnings)
    table["notes"] = table["notes"].apply("; ".join)

    sources = _build_sources(facts_by_metric, set(table["period_end"]), cik)
    return AnnualResult(name, cik, table, sources, warnings, notices, splits)


# ---------------------------------------------------------------------------
# Quality checks on the finished table
# ---------------------------------------------------------------------------
def _flag_share_count_jumps(table: pd.DataFrame, warnings: List[str]) -> None:
    """Warn if share counts still jump AFTER split adjustment."""
    previous_end, previous_shares = None, None
    for idx, row in table.iterrows():
        shares = row["diluted_shares_adjusted"]
        if pd.notna(shares) and previous_shares:
            ratio = shares / previous_shares
            if ratio >= SHARE_JUMP_RATIO or ratio <= 1 / SHARE_JUMP_RATIO:
                warnings.append(
                    f"Diluted shares changed {ratio:.2f}x between FY ending {previous_end} and FY ending "
                    f"{row['period_end']} even after split adjustment. This is not necessarily a split: "
                    f"it can come from a merger, IPO/SPAC recapitalization or a large share issuance. "
                    f"Check the filings before comparing share counts across this point."
                )
                table.at[idx, "notes"].append("share count jumped vs prior year (not a detected split)")
        if pd.notna(shares):
            previous_end, previous_shares = row["period_end"], shares


def _flag_zero_revenue(table: pd.DataFrame, warnings: List[str]) -> None:
    """Flag years with no real revenue: exactly 0 anywhere, or missing before the first revenue year.

    Typical cause: a blank-check (SPAC) shell whose SEC filings were later taken over by the
    operating business (Vertiv). Those early rows describe a different entity.
    """
    revenue = table["revenue"]
    first_real = next((i for i, r in enumerate(revenue) if pd.notna(r) and r > 0), None)
    flagged = []
    for i, (end, r) in enumerate(zip(table["period_end"], revenue)):
        is_zero = pd.notna(r) and r == 0
        is_missing_before_revenue = pd.isna(r) and first_real is not None and i < first_real
        if is_zero or is_missing_before_revenue:
            flagged.append(str(end))
            table.at[i, "notes"].append("no operating revenue: pre-merger shell company or missing data?")
    if flagged:
        warnings.append(
            f"No revenue (missing, or exactly 0) for FY ending {', '.join(flagged)}, before or without operating "
            f"revenue. That usually means a shell/blank-check company or a pre-merger entity, so those rows "
            f"are not comparable with later operating years."
        )


def _check_period_integrity(table: pd.DataFrame, warnings: List[str]) -> None:
    """Catch duplicate or overlapping fiscal periods."""
    if not table["period_end"].is_unique:
        warnings.append("Duplicate fiscal-year-end dates were found in the data.")
    ordered = table.sort_values("period_end")
    previous_end = None
    for _, row in ordered.iterrows():
        if previous_end is not None:
            if row["period_start"] <= previous_end:
                warnings.append(f"Reporting periods overlap: the year ending {row['period_end']} starts "
                                f"({row['period_start']}) on or before the previous period ended ({previous_end}).")
            elif (row["period_end"] - previous_end).days < MIN_ANNUAL_DAYS:
                warnings.append(f"Two fiscal years end only {(row['period_end'] - previous_end).days} days apart "
                                f"({previous_end} and {row['period_end']}): possible fiscal-year change or duplicate.")
        previous_end = row["period_end"]


def _flag_stale_data(table: pd.DataFrame, warnings: List[str]) -> None:
    latest = table["period_end"].max()
    age_days = (date.today() - latest).days
    if age_days > 450:
        warnings.append(f"Latest annual period in the data ended {latest} ({age_days} days ago); "
                        f"newer annual figures may not be available.")


def _build_sources(facts_by_metric: Dict[str, Dict[date, Fact]], kept_ends: set, cik: int) -> pd.DataFrame:
    rows = []
    for metric, found in facts_by_metric.items():
        for end, fact in found.items():
            if end not in kept_ends:
                continue
            rows.append({
                "metric": METRICS[metric].label,
                "period_start": fact.start,
                "period_end": fact.end,
                "value": fact.value,
                "unit": fact.unit,
                "sec_concept": fact.concept,
                "form": fact.form,
                "filed": fact.filed,
                "accession_number": fact.accn,
                "filing_url": filing_index_url(cik, fact.accn) if fact.accn else "",
                "first_filed": fact.first_filed,
                "first_reported_value": fact.first_value,
                "first_accession_number": fact.first_accn,
            })
    sources = pd.DataFrame(rows)
    if not sources.empty:
        sources = sources.sort_values(["period_end", "metric"], ascending=[False, True]).reset_index(drop=True)
    return sources
