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


def list_recent_form4_accessions(cik: int, *, since_date: Optional[str] = None,
                                  limit: int = 200) -> list[dict]:
    """Returns list of {accession, filed_date, primary_document} for Form 4 filings.

    since_date filters to filings on or after the given ISO date (inclusive).
    """
    data = edgar.fetch_json(edgar.submissions_url(cik), accept_404=True)
    if data is None:
        return []
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])

    out = []
    for form, dt, acc, doc in zip(forms, dates, accessions, primary_docs):
        if form != "4":
            continue
        if since_date and dt < since_date:
            continue
        out.append({
            "accession": acc,
            "filed_date": dt,
            "primary_document": doc,
        })
        if len(out) >= limit:
            break
    return out


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

    The submissions API gives us a 'primaryDocument' which is usually the
    human-readable HTML wrapper. The actual structured XML is a separate file,
    typically ending in .xml.
    """
    idx = edgar.fetch_json(edgar.filing_index_url(cik, accession), accept_404=True)
    if idx is None:
        return None
    for item in idx.get("directory", {}).get("item", []):
        name = item.get("name", "")
        if name.endswith(".xml") and not name.startswith("primary_doc"):
            # Skip metadata files
            if name in ("Financial_Report.xlsx", "MetaLinks.json"):
                continue
            return name
        # primary_doc.xml is the modern Form 4 XML filename
        if name == "primary_doc.xml":
            return name
    return None


def fetch_and_parse_form4(cik: int, accession: str, filed_date: str) -> list[Transaction]:
    """High-level helper: locate the XML, fetch it, parse it."""
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
                            *, since_date: str) -> dict:
    """Aggregate transactions since a given date into summary signals."""
    buys = []
    sells = []
    for tx in transactions:
        if tx.filed_date < since_date:
            continue
        if tx.is_discretionary_buy:
            buys.append(tx)
        elif tx.is_discretionary_sell:
            sells.append(tx)
    return {
        "discretionary_buys_count": len(buys),
        "discretionary_buys_value": sum(t.value for t in buys),
        "discretionary_buys_shares": sum(t.shares for t in buys),
        "discretionary_sells_count": len(sells),
        "discretionary_sells_value": sum(t.value for t in sells),
        "discretionary_sells_shares": sum(t.shares for t in sells),
        "cluster_buyers": len({t.insider_name for t in buys}),
        "cluster_sellers": len({t.insider_name for t in sells}),
        "latest_buy_date": max((t.transaction_date for t in buys), default=None),
        "latest_sell_date": max((t.transaction_date for t in sells), default=None),
    }
