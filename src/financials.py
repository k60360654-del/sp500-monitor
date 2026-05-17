"""Financial metrics from XBRL companyfacts.

We extract three YoY signals:

1. Dividend per share (DPS) growth — uses
   us-gaap:CommonStockDividendsPerShareDeclared (preferred) or
   us-gaap:CommonStockDividendsPerShareCashPaid (fallback). Trailing-twelve-
   month sums compared YoY. Initiation = $0 → positive; cut = decrease.

2. Gross margin YoY change in basis points — derived from
   us-gaap:Revenues (or us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax)
   and us-gaap:GrossProfit. We compute TTM gross profit / TTM revenue for the
   trailing four quarters and compare YoY.

3. Operating cash flow YoY change — TTM us-gaap:NetCashProvidedByUsedInOperatingActivities
   compared YoY.

All three are computed only when we have at least 8 quarters of data
(needed for two TTM windows to compare).
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


@dataclass
class FinancialSignals:
    # Dividend
    dps_ttm_latest: Optional[float] = None
    dps_ttm_prior_year: Optional[float] = None
    dps_yoy_change_pct: Optional[float] = None
    dps_initiation: bool = False
    dps_cut: bool = False
    # Gross margin
    gross_margin_latest_bps: Optional[float] = None
    gross_margin_prior_year_bps: Optional[float] = None
    gross_margin_yoy_change_bps: Optional[float] = None
    # OCF
    ocf_ttm_latest: Optional[float] = None
    ocf_ttm_prior_year: Optional[float] = None
    ocf_yoy_change_pct: Optional[float] = None


def _get_usd_series(facts: dict, concept: str) -> list[dict]:
    section = facts.get("facts", {}).get("us-gaap", {}).get(concept)
    if section is None:
        return []
    units = section.get("units", {})
    # Prefer USD; for per-share use USD/shares
    for unit_key in ("USD", "USD/shares"):
        if unit_key in units:
            return units[unit_key]
    return []


def _quarterly_observations(series: list[dict]) -> list[dict]:
    """Filter to quarterly periodic observations from 10-Q and 10-K filings.
    For YoY comparison we want observations with fiscal-period 'Q1'/'Q2'/'Q3'
    (from 10-Qs) and the implied Q4 (from 10-K annual minus Q1+Q2+Q3, but
    we sidestep that by using start/end dates instead).

    Strategy: each observation has 'start' and 'end' dates. Quarterly = end-start
    is roughly 80-100 days. Annual = roughly 350-380 days. We use quarterly
    observations for TTM computation; if quarterly aren't available we fall
    back to annual.
    """
    quarterly = []
    annual = []
    seen_periods = set()
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
        key = (o["start"], o["end"])
        # Deduplicate, preferring later-filed observations
        if key in seen_periods:
            continue
        if 60 <= days <= 100:
            quarterly.append(o)
            seen_periods.add(key)
        elif 350 <= days <= 380:
            annual.append(o)
    quarterly.sort(key=lambda x: x["end"])
    annual.sort(key=lambda x: x["end"])
    return quarterly if len(quarterly) >= 4 else annual


def _ttm_sum(quarterly: list[dict], end_idx: int) -> Optional[float]:
    """Sum the four quarterly observations ending at index end_idx (inclusive)."""
    if end_idx < 3:
        return None
    return sum(q["val"] for q in quarterly[end_idx - 3: end_idx + 1])


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

    # --- Dividends per share ---
    # DPS is reported per quarter as a single value (no aggregation). Use
    # quarterly observations and sum the trailing 4 to get TTM DPS.
    dps_series = _series_first_available(facts, DPS_CONCEPTS)
    if dps_series:
        # DPS observations are instantaneous-ish (per declared quarter), but
        # the companyfacts data has start/end. Use end-date sorting + sum 4.
        # Deduplicate by end-date, prefer latest-filed.
        by_end: dict[str, dict] = {}
        for o in dps_series:
            end = o.get("end")
            if not end:
                continue
            if end not in by_end or o.get("filed", "") > by_end[end].get("filed", ""):
                by_end[end] = o
        observations = sorted(by_end.values(), key=lambda x: x["end"])
        if len(observations) >= 8:
            latest_ttm = sum(o["val"] for o in observations[-4:])
            prior_ttm = sum(o["val"] for o in observations[-8:-4])
            out.dps_ttm_latest = latest_ttm
            out.dps_ttm_prior_year = prior_ttm
            if prior_ttm == 0 and latest_ttm > 0:
                out.dps_initiation = True
                out.dps_yoy_change_pct = 100.0
            elif prior_ttm > 0:
                out.dps_yoy_change_pct = (latest_ttm - prior_ttm) / prior_ttm * 100.0
                if latest_ttm < prior_ttm:
                    out.dps_cut = True

    # --- Gross margin ---
    rev_series = _series_first_available(facts, REVENUE_CONCEPTS)
    gp_series = _series_first_available(facts, GROSS_PROFIT_CONCEPTS)
    if rev_series and gp_series:
        rev_q = _quarterly_observations(rev_series)
        gp_q = _quarterly_observations(gp_series)
        # Build dicts by end date for alignment
        rev_by_end = {o["end"]: o["val"] for o in rev_q}
        gp_by_end = {o["end"]: o["val"] for o in gp_q}
        common_ends = sorted(set(rev_by_end) & set(gp_by_end))
        if len(common_ends) >= 8:
            latest_rev = sum(rev_by_end[e] for e in common_ends[-4:])
            latest_gp = sum(gp_by_end[e] for e in common_ends[-4:])
            prior_rev = sum(rev_by_end[e] for e in common_ends[-8:-4])
            prior_gp = sum(gp_by_end[e] for e in common_ends[-8:-4])
            if latest_rev > 0 and prior_rev > 0:
                latest_gm = latest_gp / latest_rev * 10000.0  # bps
                prior_gm = prior_gp / prior_rev * 10000.0
                out.gross_margin_latest_bps = latest_gm
                out.gross_margin_prior_year_bps = prior_gm
                out.gross_margin_yoy_change_bps = latest_gm - prior_gm

    # --- Operating cash flow ---
    ocf_series = _series_first_available(facts, OCF_CONCEPTS)
    if ocf_series:
        ocf_q = _quarterly_observations(ocf_series)
        if len(ocf_q) >= 8:
            latest_ttm = sum(o["val"] for o in ocf_q[-4:])
            prior_ttm = sum(o["val"] for o in ocf_q[-8:-4])
            out.ocf_ttm_latest = latest_ttm
            out.ocf_ttm_prior_year = prior_ttm
            if prior_ttm != 0:
                out.ocf_yoy_change_pct = (latest_ttm - prior_ttm) / abs(prior_ttm) * 100.0

    return out
