"""8-K item-based signals and NT-10K/Q late-filing detection.

The EDGAR submissions API returns an `items` field for each filing, a string
like "4.02,8.01" or just "5.07". We can score directly from this without
parsing the filing body, which keeps things fast.

Items we score:
  4.01  Changes in registrant's certifying accountant         → -2 (auditor change)
  4.02  Non-reliance on previously issued financial statements → -3 (restatement)
  5.02  Departure of directors / officers (need text for "no disagreement" detection)
  5.07  Submission of matters to a vote of security holders   → vote-tally parsing
  8.01  Other events (we look for buyback announcements)

Plus NT-10K and NT-10Q forms (late-filing notifications) → -2 each.

5.02 director-resignation classification: We rely on the actual filing text.
"No disagreement" or "no disagreement with the Company" appearing near the
resignation language is the safe-harbor phrase. Its ABSENCE on a resignation
filing is the bearish signal.

5.07 vote-tally classification: The 8-K body contains a structured table
of vote results. We use simple text extraction + regex to detect failed
say-on-pay (<70%), low auditor ratification (<95%), or high director
withhold votes (>20%).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from . import edgar, form4

log = logging.getLogger(__name__)

LATE_FILING_FORMS = {"NT 10-K", "NT 10-Q", "NT 10-K/A", "NT 10-Q/A"}
EIGHT_K_FORMS = {"8-K", "8-K/A"}


@dataclass
class EightKEvent:
    cik: int
    accession: str
    filed_date: str
    form: str
    items: list[str]
    # Specific flags we extract from the text body when relevant
    director_resigned_no_disagreement: Optional[bool] = None  # None = unknown / not 5.02
    director_resignation_detected: bool = False
    say_on_pay_pct: Optional[float] = None       # 5.07
    auditor_ratification_pct: Optional[float] = None  # 5.07
    director_max_withhold_pct: Optional[float] = None  # 5.07
    buyback_announcement: bool = False  # 8.01 / 7.01 / 2.02 with buyback language


@dataclass
class LateFiling:
    cik: int
    accession: str
    filed_date: str
    form: str  # "NT 10-K" etc


# --- 5.02 detection: director resignation with/without "no disagreement" -----

NO_DISAGREEMENT_PATTERNS = [
    re.compile(r"no\s+disagreement", re.IGNORECASE),
    re.compile(r"not\s+due\s+to\s+any\s+disagreement", re.IGNORECASE),
    re.compile(r"not\s+the\s+result\s+of\s+any\s+disagreement", re.IGNORECASE),
    re.compile(r"not\s+as\s+a\s+result\s+of\s+any\s+disagreement", re.IGNORECASE),
]

RESIGNATION_PATTERNS = [
    re.compile(r"\bresign(?:ed|ation)\b", re.IGNORECASE),
    re.compile(r"step(?:ped|ping)\s+down", re.IGNORECASE),
]


def _strip_html(html_bytes: bytes) -> str:
    """Crude but fast HTML→text. Good enough for keyword detection."""
    try:
        text = html_bytes.decode("utf-8", errors="ignore")
    except Exception:
        return ""
    # remove scripts/styles
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    # strip tags
    text = re.sub(r"<[^>]+>", " ", text)
    # decode common entities
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = re.sub(r"\s+", " ", text)
    return text


def detect_director_resignation(text: str) -> tuple[bool, Optional[bool]]:
    """Returns (resignation_detected, no_disagreement_language_present)."""
    has_resignation = any(p.search(text) for p in RESIGNATION_PATTERNS)
    if not has_resignation:
        return False, None
    has_no_disagreement = any(p.search(text) for p in NO_DISAGREEMENT_PATTERNS)
    return True, has_no_disagreement


# --- 5.07 vote-tally extraction ----------------------------------------------

# Vote tables in 8-K Item 5.07 are messy. We look for the canonical phrases
# alongside vote counts and percentages.
SAY_ON_PAY_HEADERS = re.compile(
    r"(?:say[-\s]on[-\s]pay|advisory.{0,40}executive\s+compensation|advisory\s+vote\s+on\s+(?:the\s+)?compensation)",
    re.IGNORECASE,
)
AUDITOR_RATIFY_HEADERS = re.compile(
    r"(?:ratif(?:y|ication).{0,40}(?:independent.{0,20}accounting|auditor|registered\s+public\s+accounting))",
    re.IGNORECASE,
)


def _extract_vote_pct_near(text: str, header_re: re.Pattern, window: int = 1200) -> Optional[float]:
    """Look for a header phrase and find For/Against vote counts within `window` chars.
    Return For / (For + Against) as a percentage.
    """
    m = header_re.search(text)
    if not m:
        return None
    chunk = text[m.start(): m.start() + window]
    # Find For: NNN,NNN,NNN and Against: NNN,NNN,NNN
    for_match = re.search(r"For[\s:]+([\d,]{3,})", chunk)
    against_match = re.search(r"Against[\s:]+([\d,]{3,})", chunk)
    if not for_match or not against_match:
        return None
    try:
        f = int(for_match.group(1).replace(",", ""))
        a = int(against_match.group(1).replace(",", ""))
    except ValueError:
        return None
    if f + a == 0:
        return None
    return 100.0 * f / (f + a)


def extract_say_on_pay_pct(text: str) -> Optional[float]:
    return _extract_vote_pct_near(text, SAY_ON_PAY_HEADERS, window=1500)


def extract_auditor_ratification_pct(text: str) -> Optional[float]:
    return _extract_vote_pct_near(text, AUDITOR_RATIFY_HEADERS, window=1500)


# --- 5.07 director-election: max withhold% across individual directors -------

DIRECTOR_ELECTION_HEADER = re.compile(
    r"(?:election\s+of\s+directors?|elect(?:ed|ion)\s+(?:the\s+)?(?:following\s+)?directors?|"
    r"each\s+of\s+the\s+following\s+(?:nominees|persons)\s+was\s+elected)",
    re.IGNORECASE,
)

# Headers that mark the END of the director-election section (start of next proposal)
NEXT_PROPOSAL_HEADER = re.compile(
    r"(?:advisory\s+vote\s+on|ratif(?:y|ication)\s+(?:of\s+)?(?:the\s+)?(?:appointment|selection)|"
    r"approve\s+the\s+|amend(?:ed|ment)\s+(?:and\s+restated\s+)?|"
    r"proposal\s+(?:no\.\s*)?[2-9]|item\s+[2-9]|"
    r"frequency\s+of\s+(?:future\s+)?advisory|say[-\s]on[-\s]pay)",
    re.IGNORECASE,
)

# Match a pair of comma-separated numbers (For count + Against/Withhold count)
# Both need at least 6 digits total to filter abstentions/small numbers.
# Format: "123,456,789  12,345,678" or "123,456,789\n12,345,678"
DIRECTOR_VOTE_PAIR = re.compile(
    r"([\d]{1,3}(?:,\d{3}){2,})\s+([\d]{1,3}(?:,\d{3}){1,})"
)


def extract_director_max_withhold_pct(text: str) -> Optional[float]:
    """Find Item 5.07 director-election section and compute max withhold% across directors.

    Returns the maximum withhold ratio for any single director, where
    withhold% = (against_or_withhold) / (for + against_or_withhold).

    Conservative: requires For count >= 1,000,000 (S&P 500 boards have tens of
    millions of shares voting) and For > Withhold per row.
    """
    m = DIRECTOR_ELECTION_HEADER.search(text)
    if not m:
        return None

    # Find end of director-election section: next proposal header within reasonable distance.
    start = m.start()
    search_window_start = start + 200  # skip the header itself
    search_window_end = start + 15000
    next_m = NEXT_PROPOSAL_HEADER.search(text[search_window_start:search_window_end])
    if next_m:
        chunk_end = search_window_start + next_m.start()
    else:
        chunk_end = start + 10000
    chunk = text[start:chunk_end]

    max_withhold = None
    for match in DIRECTOR_VOTE_PAIR.finditer(chunk):
        try:
            f = int(match.group(1).replace(",", ""))
            w = int(match.group(2).replace(",", ""))
        except ValueError:
            continue
        # Filter implausible rows
        if f < 1_000_000:    # too small for S&P 500 board votes
            continue
        if f < w:            # "For" must exceed "Withhold/Against" for any plausible row
            continue
        if f + w == 0:
            continue
        pct = 100.0 * w / (f + w)
        # Cap at 50% - higher means we've grabbed an unrelated number pair
        if pct > 50:
            continue
        if max_withhold is None or pct > max_withhold:
            max_withhold = pct

    return max_withhold


# --- 8.01 buyback announcements ---------------------------------------------

BUYBACK_PATTERNS = [
    re.compile(r"(?:share|stock)\s+repurchase\s+(?:program|plan|authorization)", re.IGNORECASE),
    re.compile(r"authoriz(?:e|ed|ation).{0,60}repurchas", re.IGNORECASE),
    re.compile(r"accelerated\s+share\s+repurchase", re.IGNORECASE),
    re.compile(r"\$[\d.,]+\s*(?:billion|million)\s+(?:share|stock)\s+repurchase", re.IGNORECASE),
]


def detect_buyback_announcement(text: str) -> bool:
    return any(p.search(text) for p in BUYBACK_PATTERNS)


# --- High-level: fetch & parse one 8-K --------------------------------------

def parse_8k(cik: int, filing: dict) -> EightKEvent:
    """Build EightKEvent from a filing record. Fetches body only if needed."""
    items_raw = filing.get("items", "") or ""
    items = [s.strip() for s in items_raw.split(",") if s.strip()]
    ev = EightKEvent(
        cik=cik,
        accession=filing["accession"],
        filed_date=filing["filed_date"],
        form=filing.get("form", "8-K"),
        items=items,
    )
    # Need body? Only if items include the codes we extract text from.
    needs_body = any(it in {"5.02", "5.07", "8.01", "7.01", "2.02"} for it in items)
    if not needs_body:
        return ev

    primary = filing.get("primary_document", "")
    if not primary:
        return ev
    url = edgar.filing_doc_url(cik, filing["accession"], primary)
    try:
        raw = edgar.fetch(url, accept_404=True)
    except Exception as e:
        log.warning("8-K body fetch failed %s/%s: %s", cik, filing["accession"], e)
        return ev
    if raw is None:
        return ev
    text = _strip_html(raw)

    if "5.02" in items:
        resigned, no_disagreement = detect_director_resignation(text)
        ev.director_resignation_detected = resigned
        if resigned:
            ev.director_resigned_no_disagreement = bool(no_disagreement)
    if "5.07" in items:
        ev.say_on_pay_pct = extract_say_on_pay_pct(text)
        ev.auditor_ratification_pct = extract_auditor_ratification_pct(text)
        ev.director_max_withhold_pct = extract_director_max_withhold_pct(text)
    if any(it in {"8.01", "7.01", "2.02"} for it in items):
        ev.buyback_announcement = detect_buyback_announcement(text)
    return ev


# --- Top-level orchestration ------------------------------------------------

def fetch_recent_8k_events(cik: int, *, since_date: str, limit: int = 100) -> list[EightKEvent]:
    filings = form4.list_recent_filings(cik, form_types=EIGHT_K_FORMS,
                                          since_date=since_date, limit=limit)
    return [parse_8k(cik, f) for f in filings]


def fetch_recent_late_filings(cik: int, *, since_date: str, limit: int = 20) -> list[LateFiling]:
    filings = form4.list_recent_filings(cik, form_types=LATE_FILING_FORMS,
                                          since_date=since_date, limit=limit)
    return [
        LateFiling(cik=cik, accession=f["accession"], filed_date=f["filed_date"], form=f["form"])
        for f in filings
    ]
