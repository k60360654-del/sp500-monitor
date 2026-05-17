"""13D / 13G filing parser.

Schedule 13D and 13G are filed by holders who own >5% of a class of voting
securities. 13D = active intent; 13G = passive intent (institutions).

Amendments (13D/A, 13G/A) are filed when the holding changes by >1% (13D) or
on other triggers. We want to detect increases in shares owned across
consecutive filings by the same filer to score as accumulation.

Approach: fetch the recent 13D/G filings for a CIK from the submissions API,
fetch each filing's body (HTML/text), and extract the reported ownership
amount (shares and percentage). Compare the latest amendment to the prior
filing by the same filer to determine direction.

The challenge: 13D/G filings name the SUBJECT company, not the FILER. The
EDGAR submissions API for a given CIK returns filings BY that CIK. To find
filings ABOUT a CIK (where they are the subject), we use the EDGAR full-text
search API for 13D/G filings referencing the subject CIK.

For simplicity and correctness, we use the per-subject approach: each
S&P 500 company's CIK is the "subject", and we query EDGAR for 13D/G filings
where this CIK appears as a related/subject entity.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from . import edgar

log = logging.getLogger(__name__)

FORM_TYPES_13DG = {"SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A"}


@dataclass
class ThirteenDGFiling:
    subject_cik: int
    filer_name: str
    accession: str
    form: str  # "SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A"
    filed_date: str
    shares_owned: Optional[float] = None
    pct_owned: Optional[float] = None


@dataclass
class ThirteenDGSignal:
    """Aggregated signal: did any filer increase their stake recently?"""
    any_13d_accumulation: bool
    any_13g_accumulation: bool
    latest_13d_filer: Optional[str]
    latest_13d_change_shares: Optional[float]
    latest_13d_filed_date: Optional[str]
    latest_13g_filer: Optional[str]
    latest_13g_change_shares: Optional[float]
    latest_13g_filed_date: Optional[str]
    filings_reviewed: int


# --- Body extraction --------------------------------------------------------

# Pattern for the "Aggregate Amount Beneficially Owned" line on Schedule 13
# cover pages. The value is usually a number with commas.
AGG_OWNED_PATTERNS = [
    re.compile(r"aggregate\s+amount\s+beneficially\s+owned[^0-9]{0,80}([\d,]{4,})", re.IGNORECASE),
    re.compile(r"amount\s+beneficially\s+owned[^0-9]{0,80}([\d,]{4,})", re.IGNORECASE),
]
PCT_OWNED_PATTERNS = [
    re.compile(r"percent\s+of\s+class[^0-9]{0,80}(\d+(?:\.\d+)?)\s*%", re.IGNORECASE),
    re.compile(r"percent.{0,20}represented.{0,40}([\d.]+)\s*%", re.IGNORECASE),
]
FILER_NAME_PATTERNS = [
    re.compile(r"name\s+of\s+reporting\s+person[^A-Za-z]{0,30}([A-Z][A-Za-z0-9 .,&'\-]+)(?:\s{2,}|\n|<)"),
]


def _strip_html(html_bytes: bytes) -> str:
    text = html_bytes.decode("utf-8", errors="ignore")
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = re.sub(r"\s+", " ", text)
    return text


def extract_shares_owned(text: str) -> Optional[float]:
    for p in AGG_OWNED_PATTERNS:
        m = p.search(text)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                continue
    return None


def extract_pct_owned(text: str) -> Optional[float]:
    for p in PCT_OWNED_PATTERNS:
        m = p.search(text)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    return None


def extract_filer_name(text: str) -> Optional[str]:
    for p in FILER_NAME_PATTERNS:
        m = p.search(text)
        if m:
            name = m.group(1).strip()
            # avoid catching boilerplate
            if len(name) > 80 or len(name) < 3:
                continue
            return name
    return None


# --- EDGAR full-text search for 13D/G filings naming this CIK as subject ----

def search_13dg_for_subject(subject_cik: int, *, since_date: str,
                              limit: int = 60) -> list[dict]:
    """Use EDGAR full-text search to find 13D/G filings where the given CIK
    is the subject. Returns list of filing dicts.

    Endpoint: https://efts.sec.gov/LATEST/search-index?q=&dateRange=custom&...
    Simpler: the submissions API for the subject CIK doesn't include 13D/G
    (those are filed by other parties). We use the EFTS JSON endpoint.
    """
    # EFTS search by ciks (subject), filtered by forms
    url = (
        "https://efts.sec.gov/LATEST/search-index?"
        f"q=&ciks={edgar.pad_cik(subject_cik)}"
        f"&forms=SC%2013D,SC%2013D%2FA,SC%2013G,SC%2013G%2FA"
        f"&dateRange=custom&startdt={since_date}&enddt=2030-12-31"
    )
    try:
        data = edgar.fetch_json(url, accept_404=True)
    except Exception as e:
        log.warning("EFTS search failed for %s: %s", subject_cik, e)
        return []
    if data is None:
        return []
    hits = data.get("hits", {}).get("hits", [])
    out = []
    for h in hits[:limit]:
        src = h.get("_source", {})
        adsh = h.get("_id", "")  # often "<accession>:<doc>"
        accession = adsh.split(":")[0] if ":" in adsh else adsh
        out.append({
            "accession": accession,
            "form": src.get("form", ""),
            "filed_date": src.get("file_date", ""),
            "display_names": src.get("display_names", []),
        })
    return out


def fetch_13dg_filing(subject_cik: int, hit: dict) -> Optional[ThirteenDGFiling]:
    """Fetch and parse one 13D/G filing for the given subject CIK."""
    accession = hit["accession"]
    # Filer is in display_names like ["Activist Holdings LLC  (CIK 0001234567)"]
    filer_name = ""
    if hit.get("display_names"):
        # Take the first display name; trim CIK suffix
        raw = hit["display_names"][0]
        filer_name = re.sub(r"\s*\(CIK[^)]+\)\s*$", "", raw).strip()

    # The filer's CIK is needed to build the archive URL. Pull it from display_names.
    cik_match = re.search(r"CIK\s+0*(\d+)", hit.get("display_names", [""])[0] if hit.get("display_names") else "")
    filer_cik = int(cik_match.group(1)) if cik_match else subject_cik

    # Try to get the filing index to find the primary doc
    idx = edgar.fetch_json(edgar.filing_index_url(filer_cik, accession), accept_404=True)
    if idx is None:
        return None
    primary = None
    for item in idx.get("directory", {}).get("item", []):
        name = item.get("name", "")
        if name.lower().endswith((".htm", ".html", ".txt")) and "exhibit" not in name.lower():
            primary = name
            break
    if primary is None:
        return None
    body_url = edgar.filing_doc_url(filer_cik, accession, primary)
    try:
        raw = edgar.fetch(body_url, accept_404=True)
    except Exception as e:
        log.warning("13D/G body fetch failed %s: %s", accession, e)
        return None
    if raw is None:
        return None
    text = _strip_html(raw)

    return ThirteenDGFiling(
        subject_cik=subject_cik,
        filer_name=filer_name or extract_filer_name(text) or "Unknown",
        accession=accession,
        form=hit.get("form", ""),
        filed_date=hit.get("filed_date", ""),
        shares_owned=extract_shares_owned(text),
        pct_owned=extract_pct_owned(text),
    )


def compute_13dg_signal(subject_cik: int, *, since_date: str,
                         max_filings: int = 12) -> ThirteenDGSignal:
    """Top-level: search EFTS for recent 13D/G filings on this CIK, fetch the
    most recent up to max_filings, group by filer, and detect accumulation
    (latest amendment has more shares than the prior filing by same filer).

    NOTE: This is expensive. To stay within run-time budgets, we cap
    max_filings, and only do this for companies where it might matter.
    """
    hits = search_13dg_for_subject(subject_cik, since_date=since_date, limit=max_filings)
    parsed: list[ThirteenDGFiling] = []
    for hit in hits[:max_filings]:
        f = fetch_13dg_filing(subject_cik, hit)
        if f is not None:
            parsed.append(f)

    # Group by filer name (best effort), sort each group by filed_date
    by_filer: dict[str, list[ThirteenDGFiling]] = {}
    for f in parsed:
        by_filer.setdefault(f.filer_name, []).append(f)
    for k in by_filer:
        by_filer[k].sort(key=lambda x: x.filed_date)

    signal = ThirteenDGSignal(
        any_13d_accumulation=False,
        any_13g_accumulation=False,
        latest_13d_filer=None, latest_13d_change_shares=None, latest_13d_filed_date=None,
        latest_13g_filer=None, latest_13g_change_shares=None, latest_13g_filed_date=None,
        filings_reviewed=len(parsed),
    )
    for filer, group in by_filer.items():
        if len(group) < 2:
            continue
        prior, latest = group[-2], group[-1]
        if prior.shares_owned is None or latest.shares_owned is None:
            continue
        change = latest.shares_owned - prior.shares_owned
        if change <= 0:
            continue
        is_13d = "13D" in (latest.form or "")
        if is_13d:
            if not signal.any_13d_accumulation or (
                signal.latest_13d_filed_date is None
                or latest.filed_date > signal.latest_13d_filed_date
            ):
                signal.any_13d_accumulation = True
                signal.latest_13d_filer = filer
                signal.latest_13d_change_shares = change
                signal.latest_13d_filed_date = latest.filed_date
        else:
            if not signal.any_13g_accumulation or (
                signal.latest_13g_filed_date is None
                or latest.filed_date > signal.latest_13g_filed_date
            ):
                signal.any_13g_accumulation = True
                signal.latest_13g_filer = filer
                signal.latest_13g_change_shares = change
                signal.latest_13g_filed_date = latest.filed_date

    return signal
