"""Main pipeline orchestrator.

Run with:
    python -m src.pipeline

Or with options:
    python -m src.pipeline --max-companies 50           # limit universe (for testing)
    python -m src.pipeline --insider-lookback-days 180  # override window
    python -m src.pipeline --skip-universe-refresh      # skip Wikipedia fetch
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

from . import edgar, universe, form4, shares, scoring

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
UNIVERSE_PATH = DATA_DIR / "universe.json"
TRANSACTIONS_PATH = DATA_DIR / "transactions.json"  # cached raw transactions
SHARE_COUNTS_PATH = DATA_DIR / "share_counts.json"  # cached share count series
OUTPUT_PATH = DATA_DIR / "companies.json"           # consumed by the dashboard
META_PATH = DATA_DIR / "meta.json"                  # last-run metadata


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str))


def update_form4_for_company(cik: int, *, since_date: str,
                              transactions_cache: dict) -> list[form4.Transaction]:
    """Update the cached transactions for a CIK.

    transactions_cache is keyed by str(cik) -> list of transaction dicts.
    Returns the full current list of Transaction objects for the CIK.
    """
    cik_key = str(cik)
    existing = transactions_cache.get(cik_key, [])
    known_accessions = {t["accession"] for t in existing}

    recent_filings = form4.list_recent_form4_accessions(cik, since_date=since_date)
    new_transactions: list[form4.Transaction] = []
    for filing in recent_filings:
        if filing["accession"] in known_accessions:
            continue
        try:
            txs = form4.fetch_and_parse_form4(cik, filing["accession"], filing["filed_date"])
        except Exception as e:
            log.warning("Form 4 parse failed cik=%s acc=%s: %s", cik, filing["accession"], e)
            continue
        new_transactions.extend(txs)

    # Convert to dict for caching
    merged = list(existing) + [asdict(t) for t in new_transactions]
    transactions_cache[cik_key] = merged
    # Rebuild as Transaction objects for downstream use
    return [form4.Transaction(**t) for t in merged]


def update_share_counts_for_company(cik: int, share_cache: dict) -> list[shares.QuarterlyShareCount]:
    """Refresh quarterly share counts for a CIK and update cache."""
    cik_key = str(cik)
    observations = shares.get_quarterly_share_counts(cik)
    share_cache[cik_key] = [
        {
            "period_end": o.period_end,
            "value": o.value,
            "accession": o.accession,
            "form": o.form,
            "filed_date": o.filed_date,
        }
        for o in observations
    ]
    return observations


def run(*, max_companies: int = 0, skip_universe_refresh: bool = False,
         weights: scoring.ScoreWeights = None,
         backfill_years: int = 1) -> None:
    weights = weights or scoring.ScoreWeights()
    today = date.today()
    insider_since = (today - timedelta(days=weights.insider_lookback_days)).isoformat()
    backfill_since = (today - timedelta(days=365 * backfill_years)).isoformat()

    # 1. Universe
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

    # 2. Load caches
    transactions_cache = load_json(TRANSACTIONS_PATH, {})
    share_cache = load_json(SHARE_COUNTS_PATH, {})

    # 3. Per-company: refresh data and score
    company_scores = []
    for i, m in enumerate(members):
        cik = m.get("cik")
        ticker = m["ticker"]
        if cik is None:
            log.warning("Skipping %s - no CIK", ticker)
            continue
        log.info("[%d/%d] %s (CIK %s)", i + 1, len(members), ticker, cik)
        try:
            txs = update_form4_for_company(cik, since_date=backfill_since,
                                            transactions_cache=transactions_cache)
            obs = update_share_counts_for_company(cik, share_cache)
        except Exception as e:
            log.warning("Update failed for %s: %s", ticker, e)
            continue

        summary = form4.summarize_transactions(txs, since_date=insider_since)
        qoq = shares.latest_qoq_change(obs)
        cs = scoring.score_company(
            ticker=ticker, cik=cik, name=m["name"],
            insider_summary=summary, qoq=qoq, weights=weights,
        )
        company_scores.append(cs)

        # Periodic save so a crash doesn't lose progress
        if (i + 1) % 25 == 0:
            save_json(TRANSACTIONS_PATH, transactions_cache)
            save_json(SHARE_COUNTS_PATH, share_cache)

    # 4. Final save
    save_json(TRANSACTIONS_PATH, transactions_cache)
    save_json(SHARE_COUNTS_PATH, share_cache)

    # 5. Sort & export
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
    save_json(META_PATH, {
        "last_run": today.isoformat(),
        "companies_scored": len(company_scores),
    })
    log.info("Done. %d companies scored. Output -> %s", len(company_scores), OUTPUT_PATH)


def main():
    setup_logging()
    p = argparse.ArgumentParser()
    p.add_argument("--max-companies", type=int, default=0,
                   help="Limit universe size (0 = no limit)")
    p.add_argument("--skip-universe-refresh", action="store_true")
    p.add_argument("--insider-lookback-days", type=int, default=90)
    p.add_argument("--backfill-years", type=int, default=1)
    p.add_argument("--insider-buy-weight", type=float, default=1.0)
    p.add_argument("--insider-sell-weight", type=float, default=-1.0)
    p.add_argument("--share-count-decrease-weight", type=float, default=1.0)
    p.add_argument("--share-count-increase-weight", type=float, default=-1.0)
    p.add_argument("--share-count-pct-threshold", type=float, default=1.0)
    args = p.parse_args()

    weights = scoring.ScoreWeights(
        insider_buy=args.insider_buy_weight,
        insider_sell=args.insider_sell_weight,
        share_count_decrease=args.share_count_decrease_weight,
        share_count_increase=args.share_count_increase_weight,
        share_count_pct_threshold=args.share_count_pct_threshold,
        insider_lookback_days=args.insider_lookback_days,
    )
    run(
        max_companies=args.max_companies,
        skip_universe_refresh=args.skip_universe_refresh,
        weights=weights,
        backfill_years=args.backfill_years,
    )


if __name__ == "__main__":
    main()
