"""Thin client for the Federal Register API.

Used by the action-tier ingestion path to pull primary-source records of
administration activity (executive orders, proclamations, memoranda,
presidential determinations, and significant final rules).

The Federal Register API is open (no key) and pretty permissive about
rate. We still throttle to 1 request/second between calls — this is a
courtesy, not a requirement.

Pressy v1 ingests:
- type=PRESDOCU (all presidential documents)
- type=RULE with significant=true (economically significant final rules)

We deliberately skip:
- type=PRORULE (proposed rules — too noisy and pre-decisional)
- type=NOTICE (administrative notices — usually procedural)
- non-significant rules (most of these are technical updates)
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Iterator, List, Optional

import requests

log = logging.getLogger(__name__)

API_BASE = "https://www.federalregister.gov/api/v1"

# Fields we ask for explicitly. Default field set omits subtype, the
# raw-text URL, and the `significant` flag — all of which we need.
DOCUMENT_FIELDS = (
    "document_number",
    "title",
    "type",
    "subtype",
    "abstract",
    "publication_date",
    "html_url",
    "raw_text_url",
    "agencies",
    "significant",
    "executive_order_number",
)

PER_PAGE = 100  # API max
MIN_REQUEST_INTERVAL_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 30


# Maps Federal Register `subtype` (PRESDOCU) and `type` strings to our
# canonical short type vocabulary used in pressy events.
_SUBTYPE_MAP = {
    "executive order": "executive_order",
    "proclamation": "proclamation",
    "memorandum": "memorandum",
    "determination": "presidential_determination",
    "notice": "notice",
    "letter": "letter",
    "order": "executive_order",  # FR sometimes shortens to just "Order"
}


class FederalRegisterError(RuntimeError):
    """Raised when the Federal Register API returns an unrecoverable error."""


def _normalize_type(api_type: str, subtype: Optional[str]) -> str:
    """Collapse FR (type, subtype) into our short vocabulary.

    PRESDOCU uses subtype to discriminate (Executive Order vs Proclamation
    vs Memorandum vs Determination). Rules don't use subtype meaningfully
    for our purposes."""
    if api_type == "Rule":
        return "rule"
    if api_type == "Proposed Rule":
        return "proposed_rule"
    if api_type == "Notice":
        return "notice"

    # Presidential document — discriminate by subtype.
    if subtype:
        slug = _SUBTYPE_MAP.get(subtype.strip().lower())
        if slug:
            return slug
        # Fall through to a defensive lowercase form so we don't lose
        # information if FR introduces a new subtype.
        return subtype.strip().lower().replace(" ", "_")
    return "presidential_document"


def _normalize_document(raw: dict) -> dict:
    return {
        "primary_source_id": raw.get("document_number"),
        "title": (raw.get("title") or "").strip(),
        "type": _normalize_type(raw.get("type") or "", raw.get("subtype")),
        "publication_date": raw.get("publication_date"),
        "agency_names": [
            a.get("name") for a in (raw.get("agencies") or []) if a.get("name")
        ],
        "abstract": (raw.get("abstract") or "").strip(),
        "html_url": raw.get("html_url"),
        "raw_text_url": raw.get("raw_text_url"),
        "executive_order_number": raw.get("executive_order_number"),
        "significant": raw.get("significant"),
    }


class _RateLimiter:
    """Sleeps to keep at least min_interval between request starts."""

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._last = 0.0

    def wait(self) -> None:
        elapsed = time.monotonic() - self._last
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last = time.monotonic()


_default_limiter = _RateLimiter(MIN_REQUEST_INTERVAL_SECONDS)


def _paginate(params: dict, limiter: _RateLimiter) -> Iterator[dict]:
    """Yield raw document dicts across all pages for a given query."""
    url = f"{API_BASE}/documents.json"
    page = 1
    while True:
        limiter.wait()
        q = dict(params)
        q["page"] = page
        q["per_page"] = PER_PAGE
        try:
            resp = requests.get(url, params=q, timeout=REQUEST_TIMEOUT_SECONDS)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise FederalRegisterError(f"FR API request failed: {e}") from e

        payload = resp.json()
        results = payload.get("results") or []
        for r in results:
            yield r

        if not payload.get("next_page_url"):
            break
        page += 1


def _query_params_for_type(
    fr_type: str,
    start: date,
    end: date,
    significant_only: bool = False,
) -> dict:
    """Build the URL params for one document-type query.

    Multi-valued FR conditions are encoded by repeating the parameter
    name with `[]` brackets, e.g. `conditions[type][]=PRESDOCU`. requests
    handles the bracket encoding correctly when the dict value is a
    list."""
    params = {
        "conditions[publication_date][gte]": start.isoformat(),
        "conditions[publication_date][lte]": end.isoformat(),
        "conditions[type][]": [fr_type],
        "fields[]": list(DOCUMENT_FIELDS),
    }
    if significant_only:
        params["conditions[significant]"] = "1"
    return params


def fetch_recent_documents(
    days_back: int = 7,
    end: Optional[date] = None,
    limiter: Optional[_RateLimiter] = None,
) -> List[dict]:
    """Return normalized FR documents from the last `days_back` days.

    Includes:
    - all PRESDOCU (executive orders, proclamations, memoranda,
      determinations, etc.)
    - RULE with significant=true (economically significant final rules)

    Each returned dict has the schema documented at the top of this
    file: primary_source_id, title, type, publication_date,
    agency_names, abstract, html_url, raw_text_url, etc.
    """
    if limiter is None:
        limiter = _default_limiter
    end_date = end or date.today()
    start_date = end_date - timedelta(days=days_back)

    out: List[dict] = []
    seen_ids: set = set()

    queries = [
        ("PRESDOCU", False),
        ("RULE", True),
    ]
    for fr_type, sig_only in queries:
        params = _query_params_for_type(
            fr_type, start_date, end_date, significant_only=sig_only,
        )
        try:
            for raw in _paginate(params, limiter):
                doc = _normalize_document(raw)
                psi = doc["primary_source_id"]
                if not psi or psi in seen_ids:
                    continue
                seen_ids.add(psi)
                out.append(doc)
        except FederalRegisterError as e:
            # Don't let a single bad query type kill the whole pull.
            log.warning("FR query failed for type=%s: %s", fr_type, e)

    return out


def fetch_document_text(
    raw_text_url: str,
    limiter: Optional[_RateLimiter] = None,
) -> str:
    """Pull full document text from FR's plain-text URL.

    FR provides a clean text endpoint at `raw_text_url`, so we don't
    need trafilatura or BeautifulSoup. Returns the body string with
    leading/trailing whitespace stripped.
    """
    if limiter is None:
        limiter = _default_limiter
    if not raw_text_url:
        raise FederalRegisterError("raw_text_url is empty")

    limiter.wait()
    try:
        resp = requests.get(raw_text_url, timeout=REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise FederalRegisterError(
            f"FR document fetch failed for {raw_text_url}: {e}"
        ) from e
    return resp.text.strip()
