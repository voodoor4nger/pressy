"""Pressy pipeline: ingest articles, extract events, persist.

Run with: python -m src.pipeline

One Gemini client is shared across all extractions in a run so that
its rate-limit state applies pipeline-wide. Per-article failures are
logged and counted but do not abort the run; the run record captures
fetched/extracted/error counts for later auditing.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from src import db, ingest
from src.extract import extract_event
from src.llm import GeminiClient

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "pressy.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
log = logging.getLogger("pressy.pipeline")


def _load_article_for_extraction(conn, article_id: int) -> dict:
    row = conn.execute(
        """SELECT a.id, a.url, a.title, a.body, a.published_date, s.name AS source_name
           FROM articles a
           JOIN sources s ON s.id = a.source_id
           WHERE a.id = ?""",
        (article_id,),
    ).fetchone()
    return {
        "source": row["source_name"],
        "date": row["published_date"] or "",
        "title": row["title"],
        "body": row["body"],
        "url": row["url"],
    }


def main() -> None:
    started = time.monotonic()
    conn = db.init_db(DB_PATH)
    try:
        run_id = db.start_run(conn)

        log.info("ingesting feeds...")
        new_article_ids = ingest.ingest_all(conn)
        log.info("ingested %d new articles total", len(new_article_ids))

        client = GeminiClient()
        extracted = 0
        errors = 0

        for aid in new_article_ids:
            article = _load_article_for_extraction(conn, aid)
            try:
                event = extract_event(article, client=client)
                db.save_event(conn, aid, event)
                extracted += 1
                log.info(
                    "extracted article %d (%s): mag=%s dir=%s",
                    aid, article["source"],
                    event.get("impact_magnitude"),
                    event.get("impact_direction"),
                )
            except Exception as e:
                log.warning("extraction failed for article %d: %s", aid, e)
                errors += 1

        db.finish_run(
            conn, run_id,
            fetched=len(new_article_ids),
            extracted=extracted,
            errors=errors,
        )

        elapsed = time.monotonic() - started
        print(
            f"Fetched {len(new_article_ids)}, "
            f"extracted {extracted}, "
            f"errors {errors}, "
            f"took {elapsed:.1f} seconds"
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
