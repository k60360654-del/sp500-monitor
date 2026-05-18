"""Form 4 (insider transaction) fetching and parsing.

Form 4 reports changes in beneficial ownership by directors, officers, and 10%+
owners. Each transaction has a transaction code:
  P = Open market or private purchase  (DISCRETIONARY BUY signal)
  S = Open market or private sale      (DISCRETIONARY SELL signal)
  A = Grant, award or other acquisition (not discretionary - noise)
  M = Exercise/conversion of derivative  (mechanical - noise)
  F = Tax withholding payment           (mechanical - noise)
  D = Disposition to the issuer         (mechanical - noise)
  G = Gift                              (not discretionary - noise)
  X = Option exercise                   (mechanical - noise)
  C = Conversion of derivative          (mechanical - noise)
  J = Other                             (review case by case)

For a true discretionary signal we also filter out 10b5-1 plan trades.
A Form 4 indicates 10b5-1 with the <aff10b5One>1</aff10b5One> flag,
or via a footnote referenced from the transaction.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from datetime import date, datetime
from typing import Optional, Iterable

from lxml import etree

from . import edgar

log = logging.getLogger(__name__)

DISCRETIONARY_BUY_CODES = {"P"}
DISCRETIONARY_SELL_CODES = {"S"}


@dataclass
class Transaction:
    cik: int
    accession: str
    filed_date: str
    transaction_date: str
    transaction_code: str
    shares: float
    price_per_share: float
    value: float  # shares * price
    acquired_disposed: str  # 'A' or 'D'
    is_10b5_1: bool
    insider_name: str
    is_director: bool
    is_officer: bool
    is_ten_percent_owner: bool
    officer_title: str
    is_discretionary_buy: bool
    is_discretionary_sell: bool


def list_recent_filings(cik: int, *, form_types: set[str],
                         since_date: Optional[str] = None,
                         limit: int = 500) -> list[dict]:
    """Returns list of {accession, filed_date, primary_document, items, form}
    for filings matching any of form_types.
    """
    data = edgar.fetch_json(edgar.submissions_url(cik), accept_404=True)
    if data is None:
        return []
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])
    items_arr = recent.get("items", [])

    out = []
    for i, form in enumerate(forms):
        if form not in form_types:
            continue
        dt = dates[i] if i < len(dates) else ""
        if since_date and dt < since_date:
            continue
        out.append({
            "accession": accessions[i] if i < len(accessions) else "",
            "filed_date": dt,
            "primary_document": primary_docs[i] if i < len(primary_docs) else "",
            "items": items_arr[i] if i < len(items_arr) else "",
            "form": form,
        })
        if len(out) >= limit:
            break
    return out


def list_recent_form4_accessions(cik: int, *, since_date: Optional[str] = None,
                                  limit: int = 200) -> list[dict]:
    """Backwards-compatible wrapper for Form 4 only."""
    return list_recent_filings(cik, form_types={"4"}, since_date=since_date, limit=limit)


def _text(el, xpath_expr: str) -> Optional[str]:
    found = el.find(xpath_expr)
    if found is None:
        return None
    # Form 4 schema wraps most values in <value>x</value>
    value_el = found.find("value")
    if value_el is not None and value_el.text is not None:
        return value_el.text.strip()
    if found.text is not None:
        return found.text.strip()
    return None


def _bool(el, xpath_expr: str) -> bool:
    v = _text(el, xpath_expr)
    if v is None:
        return False
    return v.strip() in ("1", "true", "True")


def parse_form4_xml(xml_bytes: bytes, cik: int, accession: str,
                     filed_date: str) -> list[Transaction]:
    """Parse a Form 4 XML document and return its non-derivative transactions."""
    try:
        root = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError as e:
        log.warning("Bad Form 4 XML %s: %s", accession, e)
        return []

    # Reporting owner info
    owner = root.find("reportingOwner")
    insider_name = ""
    if owner is not None:
        owner_id = owner.find("reportingOwnerId")
        if owner_id is not None:
            name_el = owner_id.find("rptOwnerName")
            if name_el is not None and name_el.text is not None:
                insider_name = name_el.text.strip()
        rel = owner.find("reportingOwnerRelationship")
        is_director = is_officer = is_ten_pct = False
        officer_title = ""
        if rel is not None:
            is_director = (rel.findtext("isDirector") or "").strip() in ("1", "true")
            is_officer = (rel.findtext("isOfficer") or "").strip() in ("1", "true")
            is_ten_pct = (rel.findtext("isTenPercentOwner") or "").strip() in ("1", "true")
            officer_title = (rel.findtext("officerTitle") or "").strip()
    else:
        is_director = is_officer = is_ten_pct = False
        officer_title = ""

    # Top-level 10b5-1 affirmation (newer Form 4s have this checkbox)
    top_10b5_1 = _bool(root, "aff10b5One")

    # Also gather footnote text - some filers note 10b5-1 only in footnotes
    footnote_text_by_id: dict[str, str] = {}
    footnotes_el = root.find("footnotes")
    if footnotes_el is not None:
        for fn in footnotes_el.findall("footnote"):
            fn_id = fn.get("id", "")
            text = (fn.text or "").lower()
            footnote_text_by_id[fn_id] = text
    all_footnote_text = " ".join(footnote_text_by_id.values())
    footnote_mentions_10b5_1 = "10b5-1" in all_footnote_text or "rule 10b5" in all_footnote_text

    transactions: list[Transaction] = []
    for tx in root.findall(".//nonDerivativeTransaction"):
        code = _text(tx, "transactionCoding/transactionCode") or ""
        shares_s = _text(tx, "transactionAmounts/transactionShares") or "0"
        price_s = _text(tx, "transactionAmounts/transactionPricePerShare") or "0"
        ad = _text(tx, "transactionAmounts/transactionAcquiredDisposedCode") or ""
        tx_date = _text(tx, "transactionDate") or filed_date

        try:
            shares = float(shares_s.replace(",", ""))
            price = float(price_s.replace(",", ""))
        except ValueError:
            continue

        # Determine 10b5-1 status for this specific transaction
        is_10b5_1 = top_10b5_1
        # Check transaction-level footnote references
        coding = tx.find("transactionCoding")
        if coding is not None:
            for fnref in coding.findall(".//footnoteId"):
                fn_id = fnref.get("id", "")
                if fn_id in footnote_text_by_id:
                    if "10b5-1" in footnote_text_by_id[fn_id]:
                        is_10b5_1 = True
        # Conservative fallback: any 10b5-1 mention in any footnote
        if not is_10b5_1 and footnote_mentions_10b5_1:
            is_10b5_1 = True

        is_disc_buy = (code in DISCRETIONARY_BUY_CODES and ad == "A" and not is_10b5_1)
        is_disc_sell = (code in DISCRETIONARY_SELL_CODES and ad == "D" and not is_10b5_1)

        transactions.append(Transaction(
            cik=cik,
            accession=accession,
            filed_date=filed_date,
            transaction_date=tx_date,
            transaction_code=code,
            shares=shares,
            price_per_share=price,
            value=shares * price,
            acquired_disposed=ad,
            is_10b5_1=is_10b5_1,
            insider_name=insider_name,
            is_director=is_director,
            is_officer=is_officer,
            is_ten_percent_owner=is_ten_pct,
            officer_title=officer_title,
            is_discretionary_buy=is_disc_buy,
            is_discretionary_sell=is_disc_sell,
        ))

    return transactions


def find_form4_xml_filename(cik: int, accession: str) -> Optional[str]:
    """Look in the filing's index.json to find the Form 4 XML filename.

    Slower fallback: only called when the fast path (primary_doc.xml) 404s.
    """
    idx = edgar.fetch_json(edgar.filing_index_url(cik, accession), accept_404=True)
    if idx is None:
        return None
    for item in idx.get("directory", {}).get("item", []):
        name = item.get("name", "")
        if name == "primary_doc.xml":
            return name
        if name.endswith(".xml"):
            if name in ("Financial_Report.xlsx", "MetaLinks.json"):
                continue
            return name
    return None


def fetch_and_parse_form4(cik: int, accession: str, filed_date: str) -> list[Transaction]:
    """Fetch + parse a Form 4. Fast path: try primary_doc.xml directly.
    Fall back to index.json lookup only if that 404s.
    """
    # Fast path: modern Form 4s use this standard filename
    fast_url = edgar.filing_doc_url(cik, accession, "primary_doc.xml")
    raw = edgar.fetch(fast_url, accept_404=True)
    if raw is not None:
        return parse_form4_xml(raw, cik, accession, filed_date)
    # Slow path: lookup via index.json (older or non-standard filings)
    xml_name = find_form4_xml_filename(cik, accession)
    if xml_name is None:
        log.warning("Could not locate Form 4 XML for %s/%s", cik, accession)
        return []
    url = edgar.filing_doc_url(cik, accession, xml_name)
    raw = edgar.fetch(url, accept_404=True)
    if raw is None:
        return []
    return parse_form4_xml(raw, cik, accession, filed_date)


def summarize_transactions(transactions: Iterable[Transaction],
                            *, since_date: str,
                            min_buy_value_usd: float = 10000.0,
                            min_sell_value_usd: float = 10000.0,
                            max_filings_per_insider: int = 3,
                            frequent_buyer_filings_threshold: int = 10,
                            frequent_buyer_max_filings: int = 1,
                            price_anomaly_min_ratio: float = 0.5) -> dict:
    """Aggregate transactions since a given date into summary signals.

    Filters applied for noise control:
      1. transactions with value < min_*_value_usd are classified as 'de minimis'
         and excluded from scoring counts (handles small ritual buys)
      2. transactions priced at < price_anomaly_min_ratio of the company's median
         sell-side price are classified as 'price_anomaly' - catches option
         exercises miscoded as "P" with strike price as transaction price
      3. counts are computed per (insider, filing) - multiple tranches in one
         Form 4 count as one decision
      4. an insider with more than frequent_buyer_filings_threshold filings in
         the window is treated as a programmatic buyer (e.g. TPL daily 1-share
         buys by Horizon Kinetics-affiliated directors) and capped at
         frequent_buyer_max_filings counted filings
      5. otherwise, per insider, only the largest max_filings_per_insider
         filings count
    """
    # First pass: compute median sell-side price per company (cik) for
    # price-anomaly detection. Sell prices ≈ market because insiders sell at
    # market; this gives us a free market-price proxy without external data.
    sell_prices_by_cik: dict[int, list[float]] = {}
    for tx in transactions:
        if tx.filed_date < since_date:
            continue
        if (tx.transaction_code == "S" and tx.acquired_disposed == "D"
                and tx.price_per_share > 0):
            sell_prices_by_cik.setdefault(tx.cik, []).append(tx.price_per_share)
    median_sell_price_by_cik: dict[int, float] = {}
    for cik, prices in sell_prices_by_cik.items():
        prices.sort()
        median_sell_price_by_cik[cik] = prices[len(prices) // 2]

    raw_buys: list[Transaction] = []
    raw_sells: list[Transaction] = []
    de_minimis_buys: list[Transaction] = []
    de_minimis_sells: list[Transaction] = []
    price_anomaly_buys: list[Transaction] = []
    excluded_10b5_buys: list[Transaction] = []
    excluded_10b5_sells: list[Transaction] = []

    for tx in transactions:
        if tx.filed_date < since_date:
            continue
        if tx.is_discretionary_buy:
            if tx.value < min_buy_value_usd or tx.price_per_share <= 0:
                de_minimis_buys.append(tx)
                continue
            # Price-anomaly check (option exercise miscoded as P)
            median_market = median_sell_price_by_cik.get(tx.cik)
            if (median_market is not None
                    and tx.price_per_share < price_anomaly_min_ratio * median_market):
                price_anomaly_buys.append(tx)
                continue
            raw_buys.append(tx)
        elif tx.is_discretionary_sell:
            if tx.value < min_sell_value_usd or tx.price_per_share <= 0:
                de_minimis_sells.append(tx)
            else:
                raw_sells.append(tx)
        elif tx.transaction_code == "P" and tx.acquired_disposed == "A" and tx.is_10b5_1:
            excluded_10b5_buys.append(tx)
        elif tx.transaction_code == "S" and tx.acquired_disposed == "D" and tx.is_10b5_1:
            excluded_10b5_sells.append(tx)

    # Collapse to one entry per (insider, filing), apply per-insider caps
    # (programmatic buyers get aggressive cap; others get standard cap)
    counted_buy_keys = _collapse_and_cap(
        raw_buys, max_filings_per_insider,
        frequent_threshold=frequent_buyer_filings_threshold,
        frequent_max=frequent_buyer_max_filings,
    )
    counted_sell_keys = _collapse_and_cap(
        raw_sells, max_filings_per_insider,
        frequent_threshold=frequent_buyer_filings_threshold,
        frequent_max=frequent_buyer_max_filings,
    )

    # Resolve back to the underlying transactions for value/share totals
    counted_buys = [tx for tx in raw_buys
                    if (tx.insider_name, tx.accession) in counted_buy_keys]
    counted_sells = [tx for tx in raw_sells
                     if (tx.insider_name, tx.accession) in counted_sell_keys]

    # Identify programmatic buyers (filed more than threshold times in window)
    buy_filings_by_insider: dict[str, set] = {}
    for tx in raw_buys:
        buy_filings_by_insider.setdefault(tx.insider_name, set()).add(tx.accession)
    programmatic_buyers = sorted([
        insider for insider, accs in buy_filings_by_insider.items()
        if len(accs) > frequent_buyer_filings_threshold
    ])

    return {
        # Counted (scored) signals - filings unit
        "discretionary_buys_count": len(counted_buy_keys),
        "discretionary_sells_count": len(counted_sell_keys),
        "discretionary_buys_value": sum(t.value for t in counted_buys),
        "discretionary_sells_value": sum(t.value for t in counted_sells),
        "discretionary_buys_shares": sum(t.shares for t in counted_buys),
        "discretionary_sells_shares": sum(t.shares for t in counted_sells),
        "discretionary_buys_filings": len({t.accession for t in counted_buys}),
        "discretionary_sells_filings": len({t.accession for t in counted_sells}),
        "cluster_buyers": len({t.insider_name for t in counted_buys}),
        "cluster_sellers": len({t.insider_name for t in counted_sells}),
        "latest_buy_date": max((t.transaction_date for t in counted_buys), default=None),
        "latest_sell_date": max((t.transaction_date for t in counted_sells), default=None),
        # Transparency - filtered transactions
        "raw_buys_count": len(raw_buys),
        "raw_sells_count": len(raw_sells),
        "de_minimis_buys_count": len(de_minimis_buys),
        "de_minimis_sells_count": len(de_minimis_sells),
        "price_anomaly_buys_count": len(price_anomaly_buys),
        "capped_buys_count": len(raw_buys) - len(counted_buys),
        "capped_sells_count": len(raw_sells) - len(counted_sells),
        "programmatic_buyers": programmatic_buyers,
        # 10b5-1
        "excluded_10b5_buys_count": len(excluded_10b5_buys),
        "excluded_10b5_sells_count": len(excluded_10b5_sells),
        "excluded_10b5_buys_filings": len({t.accession for t in excluded_10b5_buys}),
        "excluded_10b5_sells_filings": len({t.accession for t in excluded_10b5_sells}),
    }


def _collapse_and_cap(transactions: list[Transaction], cap: int,
                       *, frequent_threshold: int = 10,
                       frequent_max: int = 1) -> set:
    """Collapse to (insider, accession) pairs (one per filing per insider),
    then for each insider keep only the top `cap` filings ranked by total
    insider+filing value. Insiders with more than `frequent_threshold` filings
    in the window are treated as programmatic buyers and capped at
    `frequent_max` (default 1). Returns set of (insider_name, accession) keys.
    """
    # First, total value per (insider, accession) pair
    by_pair: dict[tuple[str, str], float] = {}
    for tx in transactions:
        key = (tx.insider_name, tx.accession)
        by_pair[key] = by_pair.get(key, 0.0) + tx.value

    # Group filings by insider, rank by value
    by_insider: dict[str, list[tuple[str, float]]] = {}
    for (insider, acc), value in by_pair.items():
        by_insider.setdefault(insider, []).append((acc, value))

    keep: set = set()
    for insider, filings in by_insider.items():
        # Programmatic / ritual buyer detection
        effective_cap = frequent_max if len(filings) > frequent_threshold else cap
        filings.sort(key=lambda fv: fv[1], reverse=True)
        for acc, _ in filings[:effective_cap]:
            keep.add((insider, acc))
    return keep
