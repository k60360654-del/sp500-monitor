"""Composite scoring engine.

Signal categories and default weights:

INSIDER (Form 4):
  Discretionary buy:                    +1 each
  Discretionary sell:                   -0.25 each  (asymmetric — buys more predictive)

SHARE COUNT (XBRL):
  QoQ decrease > 1%:                    +1
  QoQ decrease > 3%:                    +2  (replaces +1)
  QoQ increase > 1%:                    -1
  QoQ increase > 5%:                    -2  (replaces -1)

8-K EVENTS:
  Item 4.02 non-reliance:               -3 each
  Item 4.01 auditor change:             -2 each
  Director resignation w/o "no disagreement" language:  -2 each
  Buyback authorization announcement:   +1 each

LATE FILINGS:
  NT-10K / NT-10Q:                      -2 each

GOVERNANCE (8-K Item 5.07):
  Say-on-pay < 70%:                     -1
  Say-on-pay < 50%:                     -2  (replaces -1)
  Auditor ratification < 95%:           -1

13D / 13G:
  13G amendment with increased ownership: +1
  13D amendment with increased ownership: +2

FINANCIALS (XBRL TTM YoY):
  Dividend per share YoY ↑:             +1
  Dividend initiation:                  +2
  Dividend per share YoY ↓:             -2
  Gross margin YoY > +100 bps:          +1
  Gross margin YoY < -100 bps:          -1
  TTM OCF YoY > +5%:                    +1
  TTM OCF YoY < -5%:                    -1
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, timedelta
from typing import Optional


@dataclass
class ScoreWeights:
    insider_buy: float = 1.0
    insider_sell: float = -0.25
    insider_lookback_days: int = 90
    share_count_decrease_1pct: float = 1.0
    share_count_decrease_3pct: float = 2.0
    share_count_increase_1pct: float = -1.0
    share_count_increase_5pct: float = -2.0
    eightk_4_02_non_reliance: float = -3.0
    eightk_4_01_auditor_change: float = -2.0
    eightk_5_02_director_resigned_no_safe_harbor: float = -2.0
    eightk_buyback_announcement: float = 1.0
    eightk_lookback_days: int = 180
    late_filing_nt: float = -2.0
    say_on_pay_below_70: float = -1.0
    say_on_pay_below_50: float = -2.0
    auditor_ratification_below_95: float = -1.0
    director_withhold_above_20: float = -1.0
    # Insider transaction noise filters
    min_buy_value_usd: float = 10000.0
    min_sell_value_usd: float = 10000.0
    max_filings_per_insider: int = 3
    # Detect programmatic / ritual buyers (e.g. Horizon Kinetics-affiliated
    # directors at TPL doing near-daily buys). Anyone exceeding the threshold
    # has their counted filings reduced to `frequent_buyer_max_filings`.
    frequent_buyer_filings_threshold: int = 10
    frequent_buyer_max_filings: int = 1
    # Detect option exercises miscoded as "P" - filer reports strike price
    # rather than market. If price is < this fraction of the company's median
    # sell-side price in the window, treat as a price anomaly and exclude.
    price_anomaly_min_ratio: float = 0.5
    thirteen_g_amendment_accumulation: float = 1.0
    thirteen_d_amendment_accumulation: float = 2.0
    dps_yoy_increase: float = 1.0
    dps_initiation: float = 2.0
    dps_yoy_decrease: float = -2.0
    gross_margin_yoy_up_100bps: float = 1.0
    gross_margin_yoy_down_100bps: float = -1.0
    ocf_yoy_up_5pct: float = 1.0
    ocf_yoy_down_5pct: float = -1.0


@dataclass
class SignalContribution:
    category: str
    label: str
    weight: float
    detail: str = ""


@dataclass
class CompanyScore:
    ticker: str
    cik: int
    name: str
    composite_score: float
    insider_component: float
    share_count_component: float
    eightk_component: float
    late_filing_component: float
    governance_component: float
    thirteendg_component: float
    financials_component: float
    contributions: list
    insider_buys_count: int = 0
    insider_sells_count: int = 0
    insider_buys_value: float = 0.0
    insider_sells_value: float = 0.0
    insider_buys_filings: int = 0
    insider_sells_filings: int = 0
    excluded_10b5_buys_count: int = 0
    excluded_10b5_sells_count: int = 0
    raw_buys_count: int = 0
    raw_sells_count: int = 0
    de_minimis_buys_count: int = 0
    de_minimis_sells_count: int = 0
    capped_buys_count: int = 0
    capped_sells_count: int = 0
    cluster_buyers: int = 0
    cluster_sellers: int = 0
    share_count_pct_change: Optional[float] = None
    share_count_latest: Optional[float] = None
    share_count_prior: Optional[float] = None
    share_count_latest_period_end: Optional[str] = None
    share_count_prior_period_end: Optional[str] = None
    latest_buy_date: Optional[str] = None
    latest_sell_date: Optional[str] = None
    has_4_02: bool = False
    has_4_01: bool = False
    director_resignations_concerning: int = 0
    buyback_announcements: int = 0
    nt_filings_count: int = 0
    say_on_pay_latest_pct: Optional[float] = None
    auditor_ratification_latest_pct: Optional[float] = None
    director_max_withhold_latest_pct: Optional[float] = None
    has_13d_accumulation: bool = False
    has_13g_accumulation: bool = False
    latest_13d_filer: Optional[str] = None
    latest_13g_filer: Optional[str] = None
    dps_yoy_change_pct: Optional[float] = None
    dps_initiation: bool = False
    dps_cut: bool = False
    gross_margin_yoy_change_bps: Optional[float] = None
    ocf_yoy_change_pct: Optional[float] = None
    recent_transactions: list = field(default_factory=list)
    last_updated: str = ""


def _insider_component(summary, weights, contribs):
    buys = summary["discretionary_buys_count"]
    sells = summary["discretionary_sells_count"]
    total = buys * weights.insider_buy + sells * weights.insider_sell
    if buys:
        contribs.append(SignalContribution("insider",
            f"{buys} discretionary insider buy{'s' if buys != 1 else ''}",
            buys * weights.insider_buy,
            f"across {summary.get('discretionary_buys_filings', 0)} filing(s)"))
    if sells:
        contribs.append(SignalContribution("insider",
            f"{sells} discretionary insider sell{'s' if sells != 1 else ''}",
            sells * weights.insider_sell,
            f"across {summary.get('discretionary_sells_filings', 0)} filing(s)"))
    return total


def _share_count_component(qoq, weights, contribs):
    if qoq is None:
        return 0.0
    pct = qoq["pct_change"]
    if pct <= -3.0:
        w = weights.share_count_decrease_3pct
        contribs.append(SignalContribution("share_count",
            f"Share count decreased {pct:.2f}% QoQ (aggressive buyback)", w))
        return w
    if pct <= -1.0:
        w = weights.share_count_decrease_1pct
        contribs.append(SignalContribution("share_count",
            f"Share count decreased {pct:.2f}% QoQ (buyback)", w))
        return w
    if pct >= 5.0:
        w = weights.share_count_increase_5pct
        contribs.append(SignalContribution("share_count",
            f"Share count increased {pct:.2f}% QoQ (major dilution)", w))
        return w
    if pct >= 1.0:
        w = weights.share_count_increase_1pct
        contribs.append(SignalContribution("share_count",
            f"Share count increased {pct:.2f}% QoQ (dilution)", w))
        return w
    return 0.0


def _eightk_component(events, weights, contribs, lookback_date):
    total = 0.0
    has_4_02 = False
    has_4_01 = False
    director_concerning = 0
    buybacks = 0
    gov_say = (None, None)
    gov_aud = (None, None)
    gov_withhold = (None, None)

    for ev in events:
        if ev.filed_date < lookback_date:
            continue
        if "4.02" in ev.items:
            has_4_02 = True
            total += weights.eightk_4_02_non_reliance
            contribs.append(SignalContribution("8k",
                "8-K Item 4.02 (non-reliance on prior financials)",
                weights.eightk_4_02_non_reliance,
                f"filed {ev.filed_date} / acc {ev.accession}"))
        if "4.01" in ev.items:
            has_4_01 = True
            total += weights.eightk_4_01_auditor_change
            contribs.append(SignalContribution("8k",
                "8-K Item 4.01 (auditor change)",
                weights.eightk_4_01_auditor_change,
                f"filed {ev.filed_date} / acc {ev.accession}"))
        if ("5.02" in ev.items and ev.director_resignation_detected
                and ev.director_resigned_no_disagreement is False):
            director_concerning += 1
            total += weights.eightk_5_02_director_resigned_no_safe_harbor
            contribs.append(SignalContribution("8k",
                "Director resignation without 'no disagreement' language",
                weights.eightk_5_02_director_resigned_no_safe_harbor,
                f"filed {ev.filed_date} / acc {ev.accession}"))
        if ev.buyback_announcement:
            buybacks += 1
            total += weights.eightk_buyback_announcement
            contribs.append(SignalContribution("8k",
                "Buyback authorization announcement",
                weights.eightk_buyback_announcement,
                f"filed {ev.filed_date}"))
        if "5.07" in ev.items:
            if ev.say_on_pay_pct is not None:
                if gov_say[0] is None or ev.filed_date > gov_say[0]:
                    gov_say = (ev.filed_date, ev.say_on_pay_pct)
            if ev.auditor_ratification_pct is not None:
                if gov_aud[0] is None or ev.filed_date > gov_aud[0]:
                    gov_aud = (ev.filed_date, ev.auditor_ratification_pct)
            if ev.director_max_withhold_pct is not None:
                if gov_withhold[0] is None or ev.filed_date > gov_withhold[0]:
                    gov_withhold = (ev.filed_date, ev.director_max_withhold_pct)

    say_score = 0.0
    aud_score = 0.0
    withhold_score = 0.0
    if gov_say[1] is not None:
        if gov_say[1] < 50:
            say_score = weights.say_on_pay_below_50
            contribs.append(SignalContribution("governance",
                f"Say-on-pay vote failed badly: {gov_say[1]:.1f}%", say_score))
        elif gov_say[1] < 70:
            say_score = weights.say_on_pay_below_70
            contribs.append(SignalContribution("governance",
                f"Weak say-on-pay vote: {gov_say[1]:.1f}%", say_score))
    if gov_aud[1] is not None and gov_aud[1] < 95:
        aud_score = weights.auditor_ratification_below_95
        contribs.append(SignalContribution("governance",
            f"Low auditor ratification: {gov_aud[1]:.1f}%", aud_score))
    if gov_withhold[1] is not None and gov_withhold[1] > 20:
        withhold_score = weights.director_withhold_above_20
        contribs.append(SignalContribution("governance",
            f"Director withhold vote elevated: {gov_withhold[1]:.1f}%", withhold_score))

    return total, {
        "has_4_02": has_4_02,
        "has_4_01": has_4_01,
        "director_resignations_concerning": director_concerning,
        "buyback_announcements": buybacks,
        "say_on_pay_pct": gov_say[1],
        "auditor_ratification_pct": gov_aud[1],
        "director_max_withhold_pct": gov_withhold[1],
        "governance_total": say_score + aud_score + withhold_score,
    }


def _late_filing_component(late_filings, weights, contribs):
    if not late_filings:
        return 0.0, 0
    n = len(late_filings)
    for lf in late_filings:
        contribs.append(SignalContribution("late_filing",
            f"{lf.form} late-filing notification",
            weights.late_filing_nt,
            f"filed {lf.filed_date} / acc {lf.accession}"))
    return n * weights.late_filing_nt, n


def _thirteendg_component(signal, weights, contribs):
    if signal is None:
        return 0.0
    total = 0.0
    if signal.any_13d_accumulation:
        total += weights.thirteen_d_amendment_accumulation
        contribs.append(SignalContribution("13dg",
            f"13D amendment: {signal.latest_13d_filer} increased stake",
            weights.thirteen_d_amendment_accumulation,
            f"+{int(signal.latest_13d_change_shares or 0):,} shares, filed {signal.latest_13d_filed_date}"))
    if signal.any_13g_accumulation:
        total += weights.thirteen_g_amendment_accumulation
        contribs.append(SignalContribution("13dg",
            f"13G amendment: {signal.latest_13g_filer} increased stake",
            weights.thirteen_g_amendment_accumulation,
            f"+{int(signal.latest_13g_change_shares or 0):,} shares, filed {signal.latest_13g_filed_date}"))
    return total


def _financial_component(fs, weights, contribs):
    if fs is None:
        return 0.0
    total = 0.0
    if fs.dps_initiation:
        total += weights.dps_initiation
        contribs.append(SignalContribution("financial",
            f"Dividend initiation (TTM DPS ${fs.dps_ttm_latest:.4f})",
            weights.dps_initiation))
    elif fs.dps_cut:
        total += weights.dps_yoy_decrease
        contribs.append(SignalContribution("financial",
            f"Dividend cut YoY ({fs.dps_yoy_change_pct:+.1f}%)",
            weights.dps_yoy_decrease))
    elif fs.dps_yoy_change_pct is not None and fs.dps_yoy_change_pct > 0:
        total += weights.dps_yoy_increase
        contribs.append(SignalContribution("financial",
            f"Dividend per share grew YoY ({fs.dps_yoy_change_pct:+.1f}%)",
            weights.dps_yoy_increase))

    if fs.gross_margin_yoy_change_bps is not None:
        if fs.gross_margin_yoy_change_bps >= 100:
            total += weights.gross_margin_yoy_up_100bps
            contribs.append(SignalContribution("financial",
                f"Gross margin expanded YoY (+{fs.gross_margin_yoy_change_bps:.0f} bps)",
                weights.gross_margin_yoy_up_100bps))
        elif fs.gross_margin_yoy_change_bps <= -100:
            total += weights.gross_margin_yoy_down_100bps
            contribs.append(SignalContribution("financial",
                f"Gross margin compressed YoY ({fs.gross_margin_yoy_change_bps:.0f} bps)",
                weights.gross_margin_yoy_down_100bps))

    if fs.ocf_yoy_change_pct is not None:
        if fs.ocf_yoy_change_pct >= 5:
            total += weights.ocf_yoy_up_5pct
            contribs.append(SignalContribution("financial",
                f"TTM operating cash flow grew YoY ({fs.ocf_yoy_change_pct:+.1f}%)",
                weights.ocf_yoy_up_5pct))
        elif fs.ocf_yoy_change_pct <= -5:
            total += weights.ocf_yoy_down_5pct
            contribs.append(SignalContribution("financial",
                f"TTM operating cash flow declined YoY ({fs.ocf_yoy_change_pct:+.1f}%)",
                weights.ocf_yoy_down_5pct))
    return total


def score_company(*, ticker, cik, name,
                   insider_summary, qoq,
                   eightk_events, late_filings, thirteendg_signal, financial_signals,
                   weights, today=None):
    today = today or date.today().isoformat()
    eightk_lookback_date = (
        date.fromisoformat(today) - timedelta(days=weights.eightk_lookback_days)
    ).isoformat()

    contribs = []
    insider_c = _insider_component(insider_summary, weights, contribs)
    share_c = _share_count_component(qoq, weights, contribs)
    eightk_c, eightk_stats = _eightk_component(eightk_events or [], weights, contribs, eightk_lookback_date)
    late_c, late_n = _late_filing_component(late_filings or [], weights, contribs)
    dg_c = _thirteendg_component(thirteendg_signal, weights, contribs)
    fin_c = _financial_component(financial_signals, weights, contribs)
    gov_c = eightk_stats["governance_total"]
    composite = insider_c + share_c + eightk_c + late_c + dg_c + fin_c + gov_c

    return CompanyScore(
        ticker=ticker, cik=cik, name=name,
        composite_score=composite,
        insider_component=insider_c,
        share_count_component=share_c,
        eightk_component=eightk_c,
        late_filing_component=late_c,
        governance_component=gov_c,
        thirteendg_component=dg_c,
        financials_component=fin_c,
        contributions=[asdict(c) for c in contribs],
        insider_buys_count=insider_summary["discretionary_buys_count"],
        insider_sells_count=insider_summary["discretionary_sells_count"],
        insider_buys_value=insider_summary["discretionary_buys_value"],
        insider_sells_value=insider_summary["discretionary_sells_value"],
        insider_buys_filings=insider_summary.get("discretionary_buys_filings", 0),
        insider_sells_filings=insider_summary.get("discretionary_sells_filings", 0),
        excluded_10b5_buys_count=insider_summary.get("excluded_10b5_buys_count", 0),
        excluded_10b5_sells_count=insider_summary.get("excluded_10b5_sells_count", 0),
        raw_buys_count=insider_summary.get("raw_buys_count", 0),
        raw_sells_count=insider_summary.get("raw_sells_count", 0),
        de_minimis_buys_count=insider_summary.get("de_minimis_buys_count", 0),
        de_minimis_sells_count=insider_summary.get("de_minimis_sells_count", 0),
        capped_buys_count=insider_summary.get("capped_buys_count", 0),
        capped_sells_count=insider_summary.get("capped_sells_count", 0),
        cluster_buyers=insider_summary["cluster_buyers"],
        cluster_sellers=insider_summary["cluster_sellers"],
        share_count_pct_change=(qoq or {}).get("pct_change"),
        share_count_latest=(qoq or {}).get("latest_value"),
        share_count_prior=(qoq or {}).get("prior_value"),
        share_count_latest_period_end=(qoq or {}).get("latest_period_end"),
        share_count_prior_period_end=(qoq or {}).get("prior_period_end"),
        latest_buy_date=insider_summary.get("latest_buy_date"),
        latest_sell_date=insider_summary.get("latest_sell_date"),
        has_4_02=eightk_stats["has_4_02"],
        has_4_01=eightk_stats["has_4_01"],
        director_resignations_concerning=eightk_stats["director_resignations_concerning"],
        buyback_announcements=eightk_stats["buyback_announcements"],
        nt_filings_count=late_n,
        say_on_pay_latest_pct=eightk_stats["say_on_pay_pct"],
        auditor_ratification_latest_pct=eightk_stats["auditor_ratification_pct"],
        director_max_withhold_latest_pct=eightk_stats["director_max_withhold_pct"],
        has_13d_accumulation=getattr(thirteendg_signal, "any_13d_accumulation", False) if thirteendg_signal else False,
        has_13g_accumulation=getattr(thirteendg_signal, "any_13g_accumulation", False) if thirteendg_signal else False,
        latest_13d_filer=getattr(thirteendg_signal, "latest_13d_filer", None) if thirteendg_signal else None,
        latest_13g_filer=getattr(thirteendg_signal, "latest_13g_filer", None) if thirteendg_signal else None,
        dps_yoy_change_pct=getattr(financial_signals, "dps_yoy_change_pct", None) if financial_signals else None,
        dps_initiation=getattr(financial_signals, "dps_initiation", False) if financial_signals else False,
        dps_cut=getattr(financial_signals, "dps_cut", False) if financial_signals else False,
        gross_margin_yoy_change_bps=getattr(financial_signals, "gross_margin_yoy_change_bps", None) if financial_signals else None,
        ocf_yoy_change_pct=getattr(financial_signals, "ocf_yoy_change_pct", None) if financial_signals else None,
        last_updated=today,
    )
