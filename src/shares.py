"""Share count signal derived from XBRL companyfacts.

We use the SEC's XBRL companyfacts API which gives a time series of every
reported financial fact. For share count we use the dei (Document and Entity
Information) taxonomy concept:

    dei:EntityCommonStockSharesOutstanding

This is the "as of" share count companies report on the cover page of their
10-Q and 10-K. It's the cleanest single source. We resolve to QUARTERLY
observations by picking the value reported with the latest 10-Q / 10-K
for each fiscal period.

A small minority of filers don't populate this field consistently. As a
fallback we try:
    us-gaap:CommonStockSharesOutstanding
    us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding (last resort)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from . import edgar

log = logging.getLogger(__name__)

SHARE_COUNT_CONCEPTS_DEI = ["EntityCommonStockSharesOutstanding"]
SHARE_COUNT_CONCEPTS_GAAP = ["CommonStockSharesOutstanding"]


@dataclass
class QuarterlyShareCount:
    period_end: str  # ISO date of "as of"
    value: float
    accession: str
    form: str
    filed_date: str


def _extract_unit_series(facts_root: dict, taxonomy: str, concept: str) -> list[dict]:
    section = facts_root.get("facts", {}).get(taxonomy, {})
    entry = section.get(concept)
    if entry is None:
        return []
    units = entry.get("units", {})
    # 'shares' is the canonical unit
    return units.get("shares", []) or units.get("USD/shares", [])


def get_quarterly_share_counts(cik: int) -> list[QuarterlyShareCount]:
    """Returns a list of share count observations, one per fiscal period,
    sorted ascending by period_end date.

    Each period_end's value is taken from the latest filing reporting that
    period (i.e. an amendment supersedes the original).
    """
    data = edgar.fetch_json(edgar.companyfacts_url(cik), accept_404=True)
    if data is None:
        return []

    series: list[dict] = []
    for concept in SHARE_COUNT_CONCEPTS_DEI:
        series = _extract_unit_series(data, "dei", concept)
        if series:
            break
    if not series:
        for concept in SHARE_COUNT_CONCEPTS_GAAP:
            series = _extract_unit_series(data, "us-gaap", concept)
            if series:
                break
    if not series:
        return []

    # Keep only periodic filings (10-Q, 10-K, and their amendments)
    PERIODIC_FORMS = {"10-Q", "10-Q/A", "10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
    by_period_end: dict[str, dict] = {}
    for obs in series:
        form = obs.get("form", "")
        if form not in PERIODIC_FORMS:
            continue
        period_end = obs.get("end")
        if not period_end:
            continue
        existing = by_period_end.get(period_end)
        # Prefer the latest-filed observation for a given period
        if existing is None or obs.get("filed", "") > existing.get("filed", ""):
            by_period_end[period_end] = obs

    out: list[QuarterlyShareCount] = []
    for period_end in sorted(by_period_end.keys()):
        obs = by_period_end[period_end]
        out.append(QuarterlyShareCount(
            period_end=period_end,
            value=float(obs["val"]),
            accession=obs.get("accn", ""),
            form=obs.get("form", ""),
            filed_date=obs.get("filed", ""),
        ))
    return out


def latest_qoq_change(observations: list[QuarterlyShareCount]) -> Optional[dict]:
    """Returns the latest quarter-over-quarter change as:
        {prior_period_end, latest_period_end, prior_value, latest_value, pct_change}

    Returns None if there aren't at least two observations.
    """
    if len(observations) < 2:
        return None
    prior = observations[-2]
    latest = observations[-1]
    if prior.value <= 0:
        return None
    pct = (latest.value - prior.value) / prior.value * 100.0
    return {
        "prior_period_end": prior.period_end,
        "latest_period_end": latest.period_end,
        "prior_value": prior.value,
        "latest_value": latest.value,
        "pct_change": pct,
        "prior_form": prior.form,
        "latest_form": latest.form,
        "latest_filed_date": latest.filed_date,
    }
