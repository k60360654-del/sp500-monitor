"""Financial metrics from XBRL companyfacts.

We extract the following signals:

1. Dividend per share (DPS) growth — uses
   us-gaap:CommonStockDividendsPerShareDeclared (preferred) or
   us-gaap:CommonStockDividendsPerShareCashPaid (fallback). Trailing-twelve-
   month sums compared YoY. Initiation = $0 → positive; cut = decrease.

2. Gross margin YoY change in basis points — derived from
   us-gaap:Revenues (or us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax)
   and us-gaap:GrossProfit. TTM gross profit / TTM revenue, compared YoY.

3. **3-year OCF CAGR** (replaces the old YoY OCF signal). The previous YoY
   signal was contaminated by working-capital noise — payables stretching,
   receivables collection timing, and inventory swings can move OCF ±15% in
   a single year without any change in underlying business quality. A 3-year
   compound annual growth rate dilutes these effects. Computed as
   (TTM_now / TTM_3yr_ago)^(1/3) - 1. Requires 16 quarters of data.

4. **3-year OCF/Net Income conversion ratio** (new earnings-quality signal).
   When reported earnings consistently exceed operating cash flow, that's
   the classic earnings-quality red flag (aggressive revenue recognition,
   working-capital release that won't repeat, accrual-heavy income). We sum
   12 quarters of each and divide. Ratios well below 1.0 are negative,
   ratios above 1.10 are positive (conservative accounting).

The YoY OCF value is still computed and shown on the dashboard for context
but no longer affects the score.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from . import edgar

log = logging.getLogger(__name__)

DPS_CONCEPTS = [
    "CommonStockDividendsPerShareDeclared",
    "CommonStockDividendsPerShareCashPaid",
]
DIVIDENDS_PAID_CONCEPTS = [
    "PaymentsOfDividendsCommonStock",
    "PaymentsOfDividends",
    "DividendsCommonStockCash",
    "DividendsCommonStock",
    "Dividends",
]
REVENUE_CONCEPTS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
]
GROSS_PROFIT_CONCEPTS = ["GrossProfit"]
OCF_CONCEPTS = [
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
]
NET_INCOME_CONCEPTS = [
    "NetIncomeLoss",
    "ProfitLoss",
    "NetIncomeLossAvailableToCommonStockholdersBasic",
]


@dataclass
class FinancialSignals:
    # Dividend - sequential comparison (latest q vs prior 4q median)
    dps_latest_quarter: Optional[float] = None
    dps_prior_baseline: Optional[float] = None
    dps_change_vs_baseline_pct: Optional[float] = None
    # Dividend - TTM YoY (context/display only, not scored)
    dps_ttm_latest: Optional[float] = None
    dps_ttm_prior_year: Optional[float] = None
    dps_yoy_change_pct: Optional[float] = None
    dps_initiation: bool = False
    dps_cut: bool = False
    # Total $ dividends paid - cross-check for stock-split detection
    dividends_paid_change_vs_baseline_pct: Optional[float] = None
    dividends_paid_ttm_latest: Optional[float] = None
    dividends_paid_ttm_prior_year: Optional[float] = None
    dividends_paid_yoy_change_pct: Optional[float] = None
    dps_split_suspected: bool = False
    # Gross margin
    gross_margin_latest_bps: Optional[float] = None
    gross_margin_prior_year_bps: Optional[float] = None
    gross_margin_yoy_change_bps: Optional[float] = None
    # OCF - YoY retained for display context only (no longer scored)
    ocf_ttm_latest: Optional[float] = None
    ocf_ttm_prior_year: Optional[float] = None
    ocf_yoy_change_pct: Optional[float] = None
    # OCF - 3-year CAGR (new scored signal)
    ocf_ttm_3yr_ago: Optional[float] = None
    ocf_3yr_cagr_pct: Optional[float] = None
    # OCF/NI conversion ratio - 3-year averaged
    ocf_12q_sum: Optional[float] = None
    ni_12q_sum: Optional[float] = None
    ocf_ni_conversion_3yr: Optional[float] = None


def _get_usd_series(facts: dict, concept: str) -> list[dict]:
    section = facts.get("facts", {}).get("us-gaap", {}).get(concept)
    if section is None:
        return []
    units = section.get("units", {})
    for unit_key in ("USD", "USD/shares"):
        if unit_key in units:
            return units[unit_key]
    return []


def _quarterly_observations(series: list[dict]) -> list[dict]:
    """Filter to quarterly periodic observations (60-100 day windows). Falls
    back to annual (350-380 day) only if there's not enough quarterly data.
    Sorted by end-date ascending, deduplicated by (start,end) pair preferring
    later-filed values (handles split-adjusted restatements: a 10-K filed
    post-split restates the prior year's quarterly DPS at split-adjusted
    values, and we want those rather than the original pre-split values).
    """
    # First pass: collect best (latest-filed) observation per (start, end) tuple
    best_by_period: dict[tuple[str, str], dict] = {}
    for o in series:
        if "start" not in o or "end" not in o:
            continue
        try:
            from datetime import date as _d
            start = _d.fromisoformat(o["start"])
            end = _d.fromisoformat(o["end"])
        except (ValueError, TypeError):
            continue
        days = (end - start).days
        # Only keep quarterly or annual windows
        if not (60 <= days <= 100 or 350 <= days <= 380):
            continue
        key = (o["start"], o["end"])
        filed = o.get("filed", "")
        existing = best_by_period.get(key)
        if existing is None or filed > existing.get("filed", ""):
            best_by_period[key] = o

    # Second pass: split into quarterly vs annual buckets
    quarterly = []
    annual = []
    for o in best_by_period.values():
        from datetime import date as _d
        start = _d.fromisoformat(o["start"])
        end = _d.fromisoformat(o["end"])
        days = (end - start).days
        if 60 <= days <= 100:
            quarterly.append(o)
        elif 350 <= days <= 380:
            annual.append(o)
    quarterly.sort(key=lambda x: x["end"])
    annual.sort(key=lambda x: x["end"])
    return quarterly if len(quarterly) >= 4 else annual


def _series_first_available(facts: dict, concepts: list[str]) -> list[dict]:
    for c in concepts:
        s = _get_usd_series(facts, c)
        if s:
            return s
    return []


def compute_financial_signals(cik: int) -> FinancialSignals:
    out = FinancialSignals()
    facts = edgar.fetch_json(edgar.companyfacts_url(cik), accept_404=True)
    if facts is None:
        return out

    # --- Dividends per share — sequential comparison vs median baseline ---
    # We do NOT use TTM-YoY for DPS comparisons. TTM-YoY has fatal flaws:
    #   1. It conflates real cuts (signal we want) with year-old cuts (stale noise)
    #   2. It double-counts the transition year (a cut announced 11 months ago
    #      still shows up as a "cut" until 12 months elapse)
    #   3. It mixes special dividends from REITs/royalty trusts into the
    #      averaging, creating false positives for variable-dividend payers
    # Instead, we compare the most recent quarterly DPS to the MEDIAN of the
    # prior 4 quarters. Median is robust to special dividends. Comparison
    # against the immediate baseline catches cuts the quarter they happen and
    # stops flagging them once the new lower run-rate becomes the baseline.
    dps_series = _series_first_available(facts, DPS_CONCEPTS)
    if dps_series:
        observations = _quarterly_observations(dps_series)
        if len(observations) >= 5:
            latest_q = observations[-1]["val"]
            prior_quarters = [o["val"] for o in observations[-5:-1]]
            from statistics import median
            baseline = median(prior_quarters)
            out.dps_latest_quarter = latest_q
            out.dps_prior_baseline = baseline
            if baseline > 0:
                out.dps_change_vs_baseline_pct = (latest_q - baseline) / baseline * 100.0
            elif latest_q > 0:
                out.dps_change_vs_baseline_pct = 100.0  # initiation
        # Also retain TTM-YoY for display context only (not scored)
        if len(observations) >= 8:
            latest_ttm = sum(o["val"] for o in observations[-4:])
            prior_ttm = sum(o["val"] for o in observations[-8:-4])
            out.dps_ttm_latest = latest_ttm
            out.dps_ttm_prior_year = prior_ttm
            if prior_ttm > 0:
                out.dps_yoy_change_pct = (latest_ttm - prior_ttm) / prior_ttm * 100.0

    # --- Total dividends paid (cross-check for stock-split detection) ---
    # Same sequential comparison logic.
    paid_series = _series_first_available(facts, DIVIDENDS_PAID_CONCEPTS)
    if paid_series:
        paid_obs = _quarterly_observations(paid_series)
        if len(paid_obs) >= 5:
            latest_paid_q = paid_obs[-1]["val"]
            prior_paid_qs = [o["val"] for o in paid_obs[-5:-1]]
            from statistics import median
            paid_baseline = median(prior_paid_qs)
            if paid_baseline > 0:
                out.dividends_paid_change_vs_baseline_pct = (
                    (latest_paid_q - paid_baseline) / paid_baseline * 100.0
                )
            elif latest_paid_q > 0:
                out.dividends_paid_change_vs_baseline_pct = 100.0
        # Also retain TTM for context
        if len(paid_obs) >= 8:
            latest_paid_ttm = sum(o["val"] for o in paid_obs[-4:])
            prior_paid_ttm = sum(o["val"] for o in paid_obs[-8:-4])
            out.dividends_paid_ttm_latest = latest_paid_ttm
            out.dividends_paid_ttm_prior_year = prior_paid_ttm
            if prior_paid_ttm > 0:
                out.dividends_paid_yoy_change_pct = (
                    (latest_paid_ttm - prior_paid_ttm) / prior_paid_ttm * 100.0
                )

    # --- Determine cut / increase / initiation / split-suspicion ---
    # Use the sequential-vs-baseline comparisons, NOT the TTM-YoY values.
    psh_pct = out.dps_change_vs_baseline_pct
    paid_pct = out.dividends_paid_change_vs_baseline_pct

    # Initiation (baseline ~0, latest > 0) - reliable, splits don't fake it
    if (out.dps_prior_baseline is not None
            and out.dps_prior_baseline == 0
            and (out.dps_latest_quarter or 0) > 0):
        out.dps_initiation = True

    # Cut detection - require latest quarter ≥20% below the prior-4q median.
    # Real dividend cuts are almost always >25%; the 20% threshold gives a
    # small buffer while excluding REIT special-dividend variability (where
    # quarter-to-quarter swings of 10-15% are normal).
    if psh_pct is not None and psh_pct < -20.0:
        if paid_pct is None:
            # No total-$ cross-check available - go with per-share alone
            out.dps_cut = True
        elif paid_pct < -10.0:
            # Both per-share AND total $ confirm - real cut
            out.dps_cut = True
        else:
            # Per-share dropped sharply but total $ paid is steady/rising = split
            out.dps_split_suspected = True
            out.dps_cut = False

    # Increase detection - small threshold (2%) is fine, raises are reliable
    if psh_pct is not None and psh_pct > 2.0:
        if paid_pct is not None and paid_pct < -2.0:
            # Per-share rose but total $ fell = reverse-split artifact
            out.dps_split_suspected = True
        # Otherwise, real raise. dps_change_vs_baseline_pct is the value to use.

    # --- Gross margin ---
    rev_series = _series_first_available(facts, REVENUE_CONCEPTS)
    gp_series = _series_first_available(facts, GROSS_PROFIT_CONCEPTS)
    if rev_series and gp_series:
        rev_q = _quarterly_observations(rev_series)
        gp_q = _quarterly_observations(gp_series)
        rev_by_end = {o["end"]: o["val"] for o in rev_q}
        gp_by_end = {o["end"]: o["val"] for o in gp_q}
        common_ends = sorted(set(rev_by_end) & set(gp_by_end))
        if len(common_ends) >= 8:
            latest_rev = sum(rev_by_end[e] for e in common_ends[-4:])
            latest_gp = sum(gp_by_end[e] for e in common_ends[-4:])
            prior_rev = sum(rev_by_end[e] for e in common_ends[-8:-4])
            prior_gp = sum(gp_by_end[e] for e in common_ends[-8:-4])
            if latest_rev > 0 and prior_rev > 0:
                latest_gm = latest_gp / latest_rev * 10000.0
                prior_gm = prior_gp / prior_rev * 10000.0
                out.gross_margin_latest_bps = latest_gm
                out.gross_margin_prior_year_bps = prior_gm
                out.gross_margin_yoy_change_bps = latest_gm - prior_gm

    # --- Operating cash flow ---
    ocf_series = _series_first_available(facts, OCF_CONCEPTS)
    if ocf_series:
        ocf_q = _quarterly_observations(ocf_series)
        # YoY (display only, not scored)
        if len(ocf_q) >= 8:
            latest_ttm = sum(o["val"] for o in ocf_q[-4:])
            prior_ttm = sum(o["val"] for o in ocf_q[-8:-4])
            out.ocf_ttm_latest = latest_ttm
            out.ocf_ttm_prior_year = prior_ttm
            if prior_ttm != 0:
                out.ocf_yoy_change_pct = (latest_ttm - prior_ttm) / abs(prior_ttm) * 100.0
        # 12-quarter sum for OCF/NI conversion
        if len(ocf_q) >= 12:
            out.ocf_12q_sum = sum(o["val"] for o in ocf_q[-12:])
        # 3-year CAGR (16 quarters required)
        if len(ocf_q) >= 16:
            latest_ttm = sum(o["val"] for o in ocf_q[-4:])
            prior_ttm_3yr_ago = sum(o["val"] for o in ocf_q[-16:-12])
            out.ocf_ttm_3yr_ago = prior_ttm_3yr_ago
            # CAGR is mathematically valid only when both endpoints positive.
            # Sign-change cases get capped synthetic values so the signal can
            # still fire on a meaningful swing.
            if prior_ttm_3yr_ago > 0 and latest_ttm > 0:
                out.ocf_3yr_cagr_pct = ((latest_ttm / prior_ttm_3yr_ago) ** (1.0 / 3.0) - 1.0) * 100.0
            elif prior_ttm_3yr_ago > 0 and latest_ttm <= 0:
                out.ocf_3yr_cagr_pct = -100.0
            elif prior_ttm_3yr_ago <= 0 and latest_ttm > 0:
                out.ocf_3yr_cagr_pct = 100.0

    # --- Net Income for OCF/NI conversion ---
    ni_series = _series_first_available(facts, NET_INCOME_CONCEPTS)
    if ni_series:
        ni_q = _quarterly_observations(ni_series)
        if len(ni_q) >= 12:
            out.ni_12q_sum = sum(o["val"] for o in ni_q[-12:])

    # OCF/NI conversion ratio - only meaningful when NI is positive over the
    # 3-year window. When NI is negative the ratio inverts and misleads.
    if out.ocf_12q_sum is not None and out.ni_12q_sum is not None and out.ni_12q_sum > 0:
        out.ocf_ni_conversion_3yr = out.ocf_12q_sum / out.ni_12q_sum

    return out
