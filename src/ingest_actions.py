"""Ingest primary-source administration actions from the Federal Register.

Companion to ingest.py (which handles RSS news). This module:
- Pulls recent FR documents matching our type filters
- Skips documents we've already processed (dedup by FR document_number)
- Fetches the plain-text body
- Persists the document into the existing articles table
- Returns the new article IDs for the extractor to process

It does NOT extract events — that's pipeline_actions.py's job, which
calls extract_action_event() and then save_event(tier="action") so
each event row is tagged with both the right prompt version and
the right tier.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import List

from src import db, federal_register, ingest

log = logging.getLogger(__name__)

ACTION_SOURCE_NAME = "Federal Register"
ACTION_SOURCE_RSS = "https://www.federalregister.gov/api/v1/documents.json"
ACTION_SOURCE_BIAS = "primary"
ACTION_SOURCE_BIAS_SOURCE = "N/A"

# FR documents are usually long. Anything below this almost certainly
# means the raw text fetch returned a redirect or stub page rather than
# the document body.
MIN_BODY_CHARS = 200


def _ensure_action_source(conn: sqlite3.Connection) -> int:
    """Get or create the Federal Register source row. Returns its id."""
    return db.get_or_create_source(
        conn,
        name=ACTION_SOURCE_NAME,
        rss=ACTION_SOURCE_RSS,
        bias=ACTION_SOURCE_BIAS,
        bias_source=ACTION_SOURCE_BIAS_SOURCE,
    )


def _build_body(doc: dict, full_text: str) -> str:
    """Stitch the doc metadata + body into a single string for the LLM.

    The action prompt populates the body slot via {{ARTICLE_BODY}}, so
    the agencies list and abstract need to be inside that string. The
    LLM sees title and date through their own placeholders.
    """
    lines = []
    if doc.get("agency_names"):
        lines.append("Agencies: " + ", ".join(doc["agency_names"]))
    if doc.get("executive_order_number"):
        lines.append(f"Executive Order Number: {doc['executive_order_number']}")
    if doc.get("abstract"):
        lines.append("")
        lines.append("Abstract: " + doc["abstract"])
    if lines:
        lines.append("")  # blank line separates metadata from full text
    lines.append(full_text)
    return "\n".join(lines)


def ingest_federal_register(
    conn: sqlite3.Connection,
    days_back: int = 7,
) -> List[int]:
    """Pull FR documents from the last `days_back` days, persist new ones.

    Returns a list of newly-saved article IDs (those not previously
    seen). Per-document failures (e.g. the text endpoint 404s) are
    logged and skipped, not propagated."""
    source_id = _ensure_action_source(conn)
    docs = federal_register.fetch_recent_documents(days_back=days_back)
    log.info("FR returned %d candidate documents (days_back=%d)",
             len(docs), days_back)

    new_ids: List[int] = []
    for doc in docs:
        psi = doc.get("primary_source_id")
        if not psi:
            log.warning("FR doc missing document_number; skipping")
            continue

        if db.action_already_processed(conn, psi):
            continue

        text_url = doc.get("raw_text_url")
        if not text_url:
            log.warning("FR doc %s has no raw_text_url; skipping", psi)
            continue

        try:
            body_text = federal_register.fetch_document_text(text_url)
        except federal_register.FederalRegisterError as e:
            log.warning("text fetch failed for %s: %s", psi, e)
            continue

        if len(body_text) < MIN_BODY_CHARS:
            log.warning(
                "FR doc %s body is suspiciously short (%d chars); skipping",
                psi, len(body_text),
            )
            continue

        body = _build_body(doc, body_text)
        content_hash = ingest.compute_content_hash(body)

        # Re-check by content hash too — the same EO number shouldn't
        # exist as different rows, but defense-in-depth is cheap.
        if db.article_already_processed(conn, content_hash):
            continue

        article_id = db.save_article(
            conn,
            source_id=source_id,
            url=doc.get("html_url") or "",
            title=doc.get("title") or psi,
            body=body,
            published_date=doc.get("publication_date"),
            content_hash=content_hash,
        )
        new_ids.append(article_id)

    return new_ids
