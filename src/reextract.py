"""Re-run extraction on specific article IDs to validate prompt changes.

Usage:
    python -m src.reextract <article_id> [<article_id> ...]
    python -m src.reextract <article_id> ... --commit

Without --commit (default): dry-run. Prints the OLD stored event vs the
NEW extraction side by side and exits without touching the DB.

With --commit: also calls db.save_event(), inserting the new event row.
The old row is NOT deleted — events are append-only, and the daily
summary's get_latest_event_per_article query will surface only the
newest. The older row stays in the table as audit trail (and carries
the older prompt_version so you can tell when each row was produced).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from src import db
from src.extract import PROMPT_VERSION, extract_event
from src.llm import GeminiClient

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "pressy.db"


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m src.reextract",
        description=(
            "Re-extract one or more articles by ID. Default is dry-run "
            "(prints OLD vs NEW). Pass --commit to insert the new event."
        ),
    )
    p.add_argument(
        "article_ids", type=int, nargs="+", metavar="AID",
        help="Article IDs to re-extract (look them up in daily_summary or sqlite).",
    )
    p.add_argument(
        "--commit", action="store_true",
        help="Insert the new event row. Default is dry-run.",
    )
    return p.parse_args(argv)


def _fetch_article(conn, aid: int):
    return conn.execute(
        """SELECT a.id, a.title, a.body, a.url, a.published_date,
                  s.name AS source_name
           FROM articles a
           JOIN sources  s ON s.id = a.source_id
           WHERE a.id = ?""",
        (aid,),
    ).fetchone()


def _fetch_latest_event(conn, aid: int):
    return conn.execute(
        """SELECT id, event_title, categories, is_relevant,
                  impact_magnitude, impact_direction, confidence,
                  prompt_version, extracted_at
           FROM events
           WHERE article_id = ?
           ORDER BY id DESC
           LIMIT 1""",
        (aid,),
    ).fetchone()


def _format_old(row) -> str:
    if row is None:
        return "  OLD: (no prior event for this article)"
    return (
        f"  OLD eid={row['id']} prompt={row['prompt_version']} "
        f"extracted={row['extracted_at']}\n"
        f"      cats={row['categories']}  is_relevant={row['is_relevant']}  "
        f"mag={row['impact_magnitude']}  dir={row['impact_direction']}  "
        f"conf={row['confidence']}\n"
        f"      title: {row['event_title']}"
    )


def _format_new(event: dict) -> str:
    cats = json.dumps(event.get("categories", []))
    return (
        f"  NEW prompt={event.get('prompt_version')}\n"
        f"      cats={cats}  is_relevant={event.get('is_relevant')}  "
        f"mag={event.get('impact_magnitude')}  dir={event.get('impact_direction')}  "
        f"conf={event.get('confidence')}\n"
        f"      title: {event.get('event_title')}"
    )


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    mode = "COMMIT" if args.commit else "DRY-RUN"
    print(f"reextract: {mode}  prompt_version={PROMPT_VERSION}  "
          f"articles={args.article_ids}")
    print()

    client = GeminiClient()

    with db.connect(DB_PATH) as conn:
        for aid in args.article_ids:
            article_row = _fetch_article(conn, aid)
            if article_row is None:
                print(f"=== aid={aid} ===")
                print("  ERROR: article not found")
                print()
                continue

            print(f"=== aid={aid} | {article_row['source_name']} ===")
            print(f"  title: {article_row['title']}")

            old_row = _fetch_latest_event(conn, aid)
            print(_format_old(old_row))

            article = {
                "source": article_row["source_name"],
                "date":   article_row["published_date"] or "",
                "title":  article_row["title"],
                "body":   article_row["body"],
                "url":    article_row["url"],
            }

            try:
                new_event = extract_event(article, client=client)
            except Exception as e:
                print(f"  EXTRACTION FAILED: {e}")
                print()
                continue

            print(_format_new(new_event))

            if args.commit:
                eid = db.save_event(conn, aid, new_event)
                print(f"  COMMITTED as eid={eid}")
            else:
                print("  (dry-run; pass --commit to insert)")

            print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
