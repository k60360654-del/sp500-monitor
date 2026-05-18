"""Main pipeline orchestrator.

Usage:
    python -m src.pipeline                              # full S&P 500
    python -m src.pipeline --max-companies 50           # smoke test
    python -m src.pipeline --skip-13dg                  # disable expensive 13D/G fetching
    python -m src.pipeline --insider-lookback-days 180  # widen window
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

from . import edgar, universe, form4, shares, eightk, thirteendg, financials, scoring

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
UNIVERSE_PATH = DATA_DIR / "universe.json"
TRANSACTIONS_PATH = DATA_DIR / "transactions.json"
SHARE_COUNTS_PATH = DATA_DIR / "share_counts.json"
OUTPUT_PATH = DATA_DIR / "companies.json"
META_PATH = DATA_DIR / "meta.json"


def setup_logging():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stdout)


def load_json(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str))


def update_form4_for_company(cik, since_date, transactions_cache):
    cik_key = str(cik)
    existing = transactions_cache.get(cik_key, [])
    known_accessions = {t["accession"] for t in existing}
    recent_filings = form4.list_recent_form4_accessions(cik, since_date=since_date)
    new_transactions = []
    for filing in recent_filings:
        if filing["accession"] in known_accessions:
            continue
        try:
            txs = form4.fetch_and_parse_form4(cik, filing["accession"], filing["filed_date"])
        except Exception as e:
            log.warning("Form 4 parse failed cik=%s acc=%s: %s", cik, filing["accession"], e)
            continue
        new_transactions.extend(txs)
    merged = list(existing) + [asdict(t) for t in new_transactions]
    transactions_cache[cik_key] = merged
    return [form4.Transaction(**t) for t in merged]


def update_share_counts_for_company(cik, share_cache):
    cik_key = str(cik)
    observations = shares.get_quarterly_share_counts(cik)
    share_cache[cik_key] = [
        {"period_end": o.period_end, "value": o.value,
         "accession": o.accession, "form": o.form, "filed_date": o.filed_date}
        for o in observations
    ]
    return observations


def run(*, max_companies=0, skip_universe_refresh=False, skip_13dg=False,
         weights=None, backfill_years=1):
    weights = weights or scoring.ScoreWeights()
    today = date.today()
    insider_since = (today - timedelta(days=weights.insider_lookback_days)).isoformat()
    eightk_since = (today - timedelta(days=weights.eightk_lookback_days)).isoformat()
    backfill_since = (today - timedelta(days=365 * backfill_years)).isoformat()
    thirteendg_since = (today - timedelta(days=365 * 2)).isoformat()

    if skip_universe_refresh and UNIVERSE_PATH.exists():
        u = json.loads(UNIVERSE_PATH.read_text())
        members = u["current_members"]
        added = removed = []
    else:
        result = universe.update_universe(UNIVERSE_PATH)
        members = result["members"]
        added = result["added"]
        removed = result["removed"]

    if max_companies > 0:
        members = members[:max_companies]

    transactions_cache = load_json(TRANSACTIONS_PATH, {})
    share_cache = load_json(SHARE_COUNTS_PATH, {})

    company_scores = []
    for i, m in enumerate(members):
        cik = m.get("cik")
        ticker = m["ticker"]
        if cik is None:
            log.warning("Skipping %s - no CIK", ticker)
            continue
        log.info("[%d/%d] %s (CIK %s)", i + 1, len(members), ticker, cik)

        # Form 4 (insider transactions)
        try:
            txs = update_form4_for_company(cik, backfill_since, transactions_cache)
        except Exception as e:
            log.warning("Form 4 update failed for %s: %s", ticker, e)
            txs = []
        # Share count
        try:
            share_obs = update_share_counts_for_company(cik, share_cache)
        except Exception as e:
            log.warning("Share count update failed for %s: %s", ticker, e)
            share_obs = []
        # 8-K events
        try:
            eightk_events = eightk.fetch_recent_8k_events(cik, since_date=eightk_since)
        except Exception as e:
            log.warning("8-K fetch failed for %s: %s", ticker, e)
            eightk_events = []
        # NT-10K / NT-10Q
        try:
            late_filings = eightk.fetch_recent_late_filings(cik, since_date=eightk_since)
        except Exception as e:
            log.warning("NT-filing fetch failed for %s: %s", ticker, e)
            late_filings = []
        # 13D / 13G (optional, expensive)
        thirteendg_signal = None
        if not skip_13dg:
            try:
                thirteendg_signal = thirteendg.compute_13dg_signal(
                    cik, since_date=thirteendg_since, max_filings=10)
            except Exception as e:
                log.warning("13D/G fetch failed for %s: %s", ticker, e)
        # Financial signals
        try:
            fin_signals = financials.compute_financial_signals(cik)
        except Exception as e:
            log.warning("Financial signals failed for %s: %s", ticker, e)
            fin_signals = None

        summary = form4.summarize_transactions(
            txs, since_date=insider_since,
            min_buy_value_usd=weights.min_buy_value_usd,
            min_sell_value_usd=weights.min_sell_value_usd,
            max_filings_per_insider=weights.max_filings_per_insider,
            frequent_buyer_filings_threshold=weights.frequent_buyer_filings_threshold,
            frequent_buyer_max_filings=weights.frequent_buyer_max_filings,
            price_anomaly_min_ratio=weights.price_anomaly_min_ratio,
        )
        qoq = shares.latest_qoq_change(share_obs)

        cs = scoring.score_company(
            ticker=ticker, cik=cik, name=m["name"],
            insider_summary=summary, qoq=qoq,
            eightk_events=eightk_events, late_filings=late_filings,
            thirteendg_signal=thirteendg_signal, financial_signals=fin_signals,
            weights=weights,
        )

        # Attach recent transactions for dashboard drill-down. counted_as_buy/sell
        # reflects what *actually* counted in the score (post de-minimis filter
        # and per-insider cap), not the raw is_discretionary_* flag.
        recent = [t for t in txs if t.filed_date >= insider_since]
        recent.sort(key=lambda t: (t.transaction_date, t.filed_date), reverse=True)

        # Replay the same filter logic to identify which transactions actually
        # contribute to the score for display purposes.
        from .form4 import _collapse_and_cap

        # Median sell-side price for price-anomaly detection (same as
        # summarize_transactions logic)
        sell_prices = sorted([t.price_per_share for t in recent
                              if t.transaction_code == "S"
                              and t.acquired_disposed == "D"
                              and t.price_per_share > 0])
        median_market = sell_prices[len(sell_prices)//2] if sell_prices else None

        def _passes_buy_filters(t):
            if not t.is_discretionary_buy:
                return False
            if t.value < weights.min_buy_value_usd or t.price_per_share <= 0:
                return False
            if (median_market is not None
                    and t.price_per_share < weights.price_anomaly_min_ratio * median_market):
                return False
            return True

        raw_scored_buys = [t for t in recent if _passes_buy_filters(t)]
        raw_scored_sells = [t for t in recent if t.is_discretionary_sell
                             and t.value >= weights.min_sell_value_usd
                             and t.price_per_share > 0]
        kept_buy_keys = _collapse_and_cap(
            raw_scored_buys, weights.max_filings_per_insider,
            frequent_threshold=weights.frequent_buyer_filings_threshold,
            frequent_max=weights.frequent_buyer_max_filings,
        )
        kept_sell_keys = _collapse_and_cap(
            raw_scored_sells, weights.max_filings_per_insider,
            frequent_threshold=weights.frequent_buyer_filings_threshold,
            frequent_max=weights.frequent_buyer_max_filings,
        )

        def _is_counted_buy(t):
            return (_passes_buy_filters(t)
                    and (t.insider_name, t.accession) in kept_buy_keys)

        def _is_counted_sell(t):
            return (t.is_discretionary_sell
                    and t.value >= weights.min_sell_value_usd
                    and t.price_per_share > 0
                    and (t.insider_name, t.accession) in kept_sell_keys)

        # Tag price-anomaly transactions for the dashboard
        def _is_price_anomaly(t):
            if not t.is_discretionary_buy:
                return False
            if t.value < weights.min_buy_value_usd or t.price_per_share <= 0:
                return False
            return (median_market is not None
                    and t.price_per_share < weights.price_anomaly_min_ratio * median_market)

        # Tag programmatic buyers for the dashboard
        programmatic_set = set(summary.get("programmatic_buyers", []))

        cs.recent_transactions = [
            {"transaction_date": t.transaction_date, "filed_date": t.filed_date,
             "accession": t.accession, "transaction_code": t.transaction_code,
             "acquired_disposed": t.acquired_disposed, "shares": t.shares,
             "price": t.price_per_share, "value": t.value,
             "insider_name": t.insider_name, "officer_title": t.officer_title,
             "is_director": t.is_director, "is_officer": t.is_officer,
             "is_ten_percent_owner": t.is_ten_percent_owner,
             "is_10b5_1": t.is_10b5_1,
             "is_price_anomaly": _is_price_anomaly(t),
             "is_programmatic_buyer": t.insider_name in programmatic_set,
             "counted_as_buy": _is_counted_buy(t),
             "counted_as_sell": _is_counted_sell(t)}
            for t in recent[:60]
        ]
        company_scores.append(cs)

        if (i + 1) % 25 == 0:
            save_json(TRANSACTIONS_PATH, transactions_cache)
            save_json(SHARE_COUNTS_PATH, share_cache)

    save_json(TRANSACTIONS_PATH, transactions_cache)
    save_json(SHARE_COUNTS_PATH, share_cache)

    company_scores.sort(key=lambda c: c.composite_score, reverse=True)
    output = {
        "generated_at": today.isoformat(),
        "weights": asdict(weights),
        "universe_size": len(members),
        "companies": [asdict(c) for c in company_scores],
        "universe_changes": {
            "added": [{"ticker": a["ticker"], "name": a["name"]} for a in added] if not skip_universe_refresh else [],
            "removed": [{"ticker": r["ticker"], "name": r["name"]} for r in removed] if not skip_universe_refresh else [],
        },
    }
    save_json(OUTPUT_PATH, output)
    save_json(META_PATH, {"last_run": today.isoformat(),
                          "companies_scored": len(company_scores)})
    log.info("Done. %d companies scored.", len(company_scores))


def main():
    setup_logging()
    p = argparse.ArgumentParser()
    p.add_argument("--max-companies", type=int, default=0)
    p.add_argument("--skip-universe-refresh", action="store_true")
    p.add_argument("--skip-13dg", action="store_true",
                   help="Skip the expensive 13D/G EFTS search (recommended for first runs)")
    p.add_argument("--insider-lookback-days", type=int, default=90)
    p.add_argument("--eightk-lookback-days", type=int, default=180)
    p.add_argument("--backfill-years", type=int, default=1)
    args = p.parse_args()
    weights = scoring.ScoreWeights(
        insider_lookback_days=args.insider_lookback_days,
        eightk_lookback_days=args.eightk_lookback_days,
    )
    run(max_companies=args.max_companies,
        skip_universe_refresh=args.skip_universe_refresh,
        skip_13dg=args.skip_13dg, weights=weights,
        backfill_years=args.backfill_years)


if __name__ == "__main__":
    main()
