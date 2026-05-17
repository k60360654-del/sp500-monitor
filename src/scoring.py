"""Composite scoring.

Default weights (matching the spec given):
  Discretionary buy:                  +1 per transaction (within lookback window)
  Discretionary sell:                 -1 per transaction
  Share count QoQ decrease > 1%:      +1
  Share count QoQ increase > 1%:      -1

All weights are configurable. Each signal contributes independently to the
composite, but is also exposed separately so the dashboard can show what
drove the score.

The insider signal uses a 90-day window by default (recent activity matters
more) and counts unique transaction events, not dollars. You can switch to
a value-weighted variant by changing `insider_mode`.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, timedelta
from typing import Optional


@dataclass
class ScoreWeights:
    insider_buy: float = 1.0
    insider_sell: float = -1.0
    share_count_decrease: float = 1.0  # applied if QoQ change < -threshold
    share_count_increase: float = -1.0  # applied if QoQ change > +threshold
    share_count_pct_threshold: float = 1.0  # percent
    insider_lookback_days: int = 90
    insider_mode: str = "count"  # "count" or "net_count" or "net_value"


@dataclass
class CompanyScore:
    ticker: str
    cik: int
    name: str
    composite_score: float
    insider_component: float
    share_count_component: float
    insider_buys_count: int
    insider_sells_count: int
    insider_buys_value: float
    insider_sells_value: float
    cluster_buyers: int
    cluster_sellers: int
    share_count_pct_change: Optional[float]
    share_count_latest: Optional[float]
    share_count_prior: Optional[float]
    share_count_latest_period_end: Optional[str]
    share_count_prior_period_end: Optional[str]
    latest_buy_date: Optional[str]
    latest_sell_date: Optional[str]
    last_updated: str


def compute_insider_component(summary: dict, weights: ScoreWeights) -> float:
    if weights.insider_mode == "count":
        return (
            summary["discretionary_buys_count"] * weights.insider_buy
            + summary["discretionary_sells_count"] * weights.insider_sell
        )
    if weights.insider_mode == "net_count":
        net = summary["discretionary_buys_count"] - summary["discretionary_sells_count"]
        return net * (weights.insider_buy if net >= 0 else -weights.insider_sell)
    if weights.insider_mode == "net_value":
        net_v = summary["discretionary_buys_value"] - summary["discretionary_sells_value"]
        # normalize per $1M
        return (net_v / 1_000_000.0) * weights.insider_buy
    return 0.0


def compute_share_count_component(qoq: Optional[dict], weights: ScoreWeights) -> float:
    if qoq is None:
        return 0.0
    pct = qoq["pct_change"]
    thr = weights.share_count_pct_threshold
    if pct < -thr:
        return weights.share_count_decrease
    if pct > thr:
        return weights.share_count_increase
    return 0.0


def score_company(*, ticker: str, cik: int, name: str,
                   insider_summary: dict, qoq: Optional[dict],
                   weights: ScoreWeights, today: Optional[str] = None) -> CompanyScore:
    today = today or date.today().isoformat()
    insider_c = compute_insider_component(insider_summary, weights)
    share_c = compute_share_count_component(qoq, weights)
    return CompanyScore(
        ticker=ticker,
        cik=cik,
        name=name,
        composite_score=insider_c + share_c,
        insider_component=insider_c,
        share_count_component=share_c,
        insider_buys_count=insider_summary["discretionary_buys_count"],
        insider_sells_count=insider_summary["discretionary_sells_count"],
        insider_buys_value=insider_summary["discretionary_buys_value"],
        insider_sells_value=insider_summary["discretionary_sells_value"],
        cluster_buyers=insider_summary["cluster_buyers"],
        cluster_sellers=insider_summary["cluster_sellers"],
        share_count_pct_change=(qoq or {}).get("pct_change"),
        share_count_latest=(qoq or {}).get("latest_value"),
        share_count_prior=(qoq or {}).get("prior_value"),
        share_count_latest_period_end=(qoq or {}).get("latest_period_end"),
        share_count_prior_period_end=(qoq or {}).get("prior_period_end"),
        latest_buy_date=insider_summary.get("latest_buy_date"),
        latest_sell_date=insider_summary.get("latest_sell_date"),
        last_updated=today,
    )
