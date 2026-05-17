"""EDGAR HTTP client.

Handles the SEC's rate limit (10 req/sec) and User-Agent requirement.
Reads the User-Agent from the EDGAR_USER_AGENT env var. The SEC requires
this to be set to your name and email address.
"""
from __future__ import annotations

import os
import time
import logging
from typing import Optional

import requests

log = logging.getLogger(__name__)

USER_AGENT = os.environ.get(
    "EDGAR_USER_AGENT",
    "SP500 Monitor research@example.com",  # override via env
)

# SEC asks for max 10 req/sec. We use a slightly lower target to be polite.
RATE_LIMIT_PER_SEC = 8.0
_MIN_INTERVAL = 1.0 / RATE_LIMIT_PER_SEC

_session: Optional[requests.Session] = None
_last_request_ts: float = 0.0


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "gzip, deflate",
            "Host": None,  # will be set per request automatically
        })
        _session = s
    return _session


def _throttle() -> None:
    global _last_request_ts
    now = time.time()
    elapsed = now - _last_request_ts
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)
    _last_request_ts = time.time()


def fetch(url: str, *, retries: int = 3, accept_404: bool = False) -> Optional[bytes]:
    """Fetch a URL from EDGAR. Returns bytes, or None on 404 if accept_404."""
    session = _get_session()
    last_exc: Optional[Exception] = None
    for attempt in range(retries):
        _throttle()
        try:
            resp = session.get(url, timeout=30)
            if resp.status_code == 404 and accept_404:
                return None
            if resp.status_code == 429:
                # backoff and retry
                sleep_s = 2.0 * (attempt + 1)
                log.warning("EDGAR 429; backing off %ss", sleep_s)
                time.sleep(sleep_s)
                continue
            resp.raise_for_status()
            return resp.content
        except requests.RequestException as e:
            last_exc = e
            log.warning("EDGAR fetch failed (attempt %d): %s", attempt + 1, e)
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"EDGAR fetch failed after {retries} retries: {url}") from last_exc


def fetch_json(url: str, **kwargs):
    import json
    raw = fetch(url, **kwargs)
    if raw is None:
        return None
    return json.loads(raw.decode("utf-8"))


def pad_cik(cik: int | str) -> str:
    """Return zero-padded 10-digit CIK string."""
    return str(int(cik)).zfill(10)


def submissions_url(cik: int | str) -> str:
    return f"https://data.sec.gov/submissions/CIK{pad_cik(cik)}.json"


def companyfacts_url(cik: int | str) -> str:
    return f"https://data.sec.gov/api/xbrl/companyfacts/CIK{pad_cik(cik)}.json"


def filing_doc_url(cik: int | str, accession_no: str, filename: str) -> str:
    """Build the archive URL for a filing's primary document.

    Note: uses the UNPADDED CIK in the directory path.
    """
    unpadded = str(int(cik))
    acc = accession_no.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{unpadded}/{acc}/{filename}"


def filing_index_url(cik: int | str, accession_no: str) -> str:
    """Build the URL to a filing's index json (lists all documents)."""
    unpadded = str(int(cik))
    acc = accession_no.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{unpadded}/{acc}/index.json"


def company_tickers_url() -> str:
    return "https://www.sec.gov/files/company_tickers.json"
