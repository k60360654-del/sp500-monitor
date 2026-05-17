"""S&P 500 universe management.

Scrapes the current S&P 500 constituent list from Wikipedia, then maps each
ticker to its SEC CIK using the SEC's company_tickers.json.

Maintains a local universe.json with:
  - current_members: list of {ticker, name, cik, added_date}
  - removed_members: list of {ticker, name, cik, added_date, removed_date}

Each pipeline run diffs Wikipedia against the local state and emits events.
"""
from __future__ import annotations

import json
import re
import logging
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

from . import edgar

log = logging.getLogger(__name__)

WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"


@dataclass
class Member:
    ticker: str
    name: str
    cik: Optional[int]
    added_date: str
    removed_date: Optional[str] = None


def fetch_sp500_from_wikipedia() -> list[dict]:
    """Returns list of {ticker, name} for current S&P 500 members."""
    headers = {"User-Agent": edgar.USER_AGENT}
    resp = requests.get(WIKIPEDIA_URL, headers=headers, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "html.parser")
    table = soup.find("table", {"id": "constituents"})
    if table is None:
        # fallback: first sortable wikitable
        table = soup.find("table", class_=re.compile(r"wikitable"))
    if table is None:
        raise RuntimeError("Could not find S&P 500 table on Wikipedia")

    rows = table.find("tbody").find_all("tr")
    members = []
    for row in rows:
        cells = row.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        ticker_cell = cells[0]
        name_cell = cells[1]
        ticker = ticker_cell.get_text(strip=True)
        name = name_cell.get_text(strip=True)
        if not ticker or ticker == "Symbol":
            continue
        # Normalize tickers - Wikipedia uses BRK.B, SEC uses BRK-B
        ticker = ticker.replace(".", "-")
        members.append({"ticker": ticker, "name": name})
    return members


def build_ticker_to_cik_map() -> dict[str, int]:
    """Fetches SEC's company_tickers.json and returns {ticker: cik} mapping."""
    data = edgar.fetch_json(edgar.company_tickers_url())
    out = {}
    # company_tickers.json is dict-of-dicts: {"0": {"cik_str": ..., "ticker": ..., "title": ...}}
    for _, entry in data.items():
        ticker = entry["ticker"].upper().replace(".", "-")
        out[ticker] = int(entry["cik_str"])
    return out


def update_universe(universe_path: Path) -> dict:
    """Update the local universe file from Wikipedia + SEC.

    Returns a dict with:
      - added: list of newly added members
      - removed: list of removed members
      - members: full current member list
    """
    today = date.today().isoformat()

    # Load existing
    if universe_path.exists():
        existing = json.loads(universe_path.read_text())
    else:
        existing = {"current_members": [], "removed_members": []}

    current_by_ticker = {m["ticker"]: m for m in existing["current_members"]}
    removed_by_ticker = {m["ticker"]: m for m in existing["removed_members"]}

    # Fetch latest from Wikipedia
    wiki_members = fetch_sp500_from_wikipedia()
    wiki_tickers = {m["ticker"] for m in wiki_members}

    # Fetch CIK map
    ticker_to_cik = build_ticker_to_cik_map()

    new_current = []
    added = []
    removed = []

    for wm in wiki_members:
        ticker = wm["ticker"]
        if ticker in current_by_ticker:
            # carry over existing record
            existing_record = current_by_ticker[ticker]
            new_current.append(existing_record)
        elif ticker in removed_by_ticker:
            # re-added after removal
            record = dict(removed_by_ticker[ticker])
            record["added_date"] = today
            record["removed_date"] = None
            new_current.append(record)
            added.append(record)
        else:
            # genuinely new
            record = {
                "ticker": ticker,
                "name": wm["name"],
                "cik": ticker_to_cik.get(ticker),
                "added_date": today,
                "removed_date": None,
            }
            if record["cik"] is None:
                log.warning("No CIK found for ticker %s (%s)", ticker, wm["name"])
            new_current.append(record)
            added.append(record)

    # Find removed: in existing current but not in Wikipedia
    new_removed = list(existing["removed_members"])
    for ticker, record in current_by_ticker.items():
        if ticker not in wiki_tickers:
            r = dict(record)
            r["removed_date"] = today
            new_removed.append(r)
            removed.append(r)

    out = {
        "updated_at": today,
        "current_members": new_current,
        "removed_members": new_removed,
    }
    universe_path.parent.mkdir(parents=True, exist_ok=True)
    universe_path.write_text(json.dumps(out, indent=2))

    log.info("Universe updated: %d current, %d added, %d removed",
             len(new_current), len(added), len(removed))
    return {"added": added, "removed": removed, "members": new_current}
