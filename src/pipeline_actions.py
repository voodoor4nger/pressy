"""Pressy action-tier pipeline: ingest Federal Register documents,
extract events, persist as tier='action'.

Run with:
    python -m src.pipeline_actions
    python -m src.pipeline_actions --days-back 30

Separate from src.pipeline (the news / framing pipeline) so the two
can run on independent cadences and so problems with one don't bleed
into the other. One Gemini client is shared across all extractions in
a single run for rate-limiting purposes.

Per-document failures are logged and counted but do not abort the run.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from pathlib import Path
from typing import List, Optional

from src import db, ingest_actions
from src.extract import extract_action_event
from src.llm import GeminiClient

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "pressy.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
log = logging.getLogger("pressy.pipeline_actions")


def _load_article_for_extraction(conn: sqlite3.Connection, article_id: int) -> dict:
    """Load a saved FR article in extract_event()'s expected shape.

    For action documents we pass the FR doc type as `source` (since the
    extract_action prompt treats the source slot as the document type)
    and the publication date as `date`. The body already contains the
    agencies / abstract preamble assembled in ingest_actions.
    """
    row = conn.execute(
        """SELECT a.id, a.url, a.title, a.body, a.published_date,
                  s.name AS source_name
           FROM articles a
           JOIN sources s ON s.id = a.source_id
           WHERE a.id = ?""",
        (article_id,),
    ).fetchone()
    return {
        "source": row["source_name"],   # "Federal Register"
        "date": row["published_date"] or "",
        "title": row["title"],
        "body": row["body"],
        "url": row["url"],
    }


def _primary_source_id_for_article(conn: sqlite3.Connection, article_id: int) -> Optional[str]:
    """Recover the FR document number from the article's html_url.

    We don't store it on the article row itself — the existing articles
    schema is shared with the news pipeline. The FR html_url contains
    the document_number as the second-to-last path segment, and we'll
    persist it on the event row instead via primary_source_id.
    """
    row = conn.execute(
        "SELECT url FROM articles WHERE id = ?", (article_id,)
    ).fetchone()
    if row is None or not row["url"]:
        return None
    url = row["url"].rstrip("/")
    # Example: https://www.federalregister.gov/documents/2026/04/24/2026-08126/eligibility-...
    parts = url.split("/")
    # Look for the segment that matches FR's NNNN-NNNNN document-number shape.
    for seg in parts:
        if len(seg) >= 7 and seg[4] == "-" and seg[:4].isdigit():
            return seg
    return None


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m src.pipeline_actions",
        description="Ingest and extract Federal Register documents.",
    )
    p.add_argument(
        "--days-back", type=int, default=7,
        help="How many days of FR history to pull (default 7).",
    )
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    started = time.monotonic()

    conn = db.init_db(DB_PATH)
    try:
        run_id = db.start_run(conn)

        log.info("ingesting Federal Register (days_back=%d)...", args.days_back)
        new_article_ids = ingest_actions.ingest_federal_register(
            conn, days_back=args.days_back,
        )
        log.info("ingested %d new FR documents", len(new_article_ids))

        client = GeminiClient()
        extracted = 0
        errors = 0

        for aid in new_article_ids:
            article = _load_article_for_extraction(conn, aid)
            psi = _primary_source_id_for_article(conn, aid)
            try:
                event = extract_action_event(article, client=client)
                db.save_event(
                    conn, aid, event,
                    tier="action",
                    primary_source_id=psi,
                )
                extracted += 1
                log.info(
                    "extracted FR doc %d (%s): mag=%s dir=%s cats=%s",
                    aid, psi or "?",
                    event.get("impact_magnitude"),
                    event.get("impact_direction"),
                    event.get("categories"),
                )
            except Exception as e:
                log.warning("extraction failed for FR doc %d (%s): %s", aid, psi, e)
                errors += 1

        db.finish_run(
            conn, run_id,
            fetched=len(new_article_ids),
            extracted=extracted,
            errors=errors,
        )

        elapsed = time.monotonic() - started
        print(
            f"Fetched {len(new_article_ids)} FR docs, "
            f"extracted {extracted}, "
            f"errors {errors}, "
            f"took {elapsed:.1f} seconds"
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
