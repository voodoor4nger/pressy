"""Re-run extraction on stored articles to validate or migrate prompt changes.

Two modes:

    # Single / a few specific articles
    python -m src.reextract <article_id> [<article_id> ...]
    python -m src.reextract <article_id> ... --commit

    # Bulk: every article whose LATEST event was extracted under VERSION
    python -m src.reextract --all-version v2
    python -m src.reextract --all-version v2 --commit
    python -m src.reextract --all-version v2 --commit --confirm   # required if >50

Without --commit (default): dry-run. Prints OLD stored event vs NEW
extraction side by side and exits without touching the DB.

With --commit: also calls db.save_event() per article. The old event
row is NOT deleted — events are append-only, and downstream queries use
get_latest_event_per_article() to surface only the newest.

Bulk safety: if --all-version would touch more than 50 articles AND
--commit is set, --confirm is required. The dry-run path has no such
guard — feel free to inspect any size sweep before committing.

We scope --all-version to articles whose LATEST event matches VERSION
(not every row), since re-extracting an article that already has a
newer event would just append a redundant row.
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

BULK_CONFIRM_THRESHOLD = 50


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m src.reextract",
        description=(
            "Re-extract one or more articles. Default is dry-run "
            "(prints OLD vs NEW). Pass --commit to insert the new event."
        ),
    )
    p.add_argument(
        "article_ids", type=int, nargs="*", default=[], metavar="AID",
        help="Article IDs to re-extract.",
    )
    p.add_argument(
        "--all-version", metavar="VERSION", default=None,
        help=(
            "Re-extract every article whose latest event was produced "
            "under this prompt version (e.g. 'v2')."
        ),
    )
    p.add_argument(
        "--commit", action="store_true",
        help="Insert the new event row. Default is dry-run.",
    )
    p.add_argument(
        "--confirm", action="store_true",
        help=(
            f"Required with --commit when --all-version would touch more "
            f"than {BULK_CONFIRM_THRESHOLD} articles."
        ),
    )
    args = p.parse_args(argv)

    if args.article_ids and args.all_version is not None:
        p.error("pass either article IDs or --all-version, not both")
    if not args.article_ids and args.all_version is None:
        p.error("provide article IDs or --all-version VERSION")
    return args


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


def _fetch_article_ids_at_version(conn, version: str) -> List[int]:
    rows = conn.execute(
        """
        SELECT e.article_id
        FROM events e
        INNER JOIN (
            SELECT article_id, MAX(id) AS max_id
            FROM events
            GROUP BY article_id
        ) latest ON e.id = latest.max_id
        WHERE e.prompt_version = ?
        ORDER BY e.article_id
        """,
        (version,),
    ).fetchall()
    return [r["article_id"] for r in rows]


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


def _diff_summary(old_row, new_event: dict) -> List[str]:
    """Compact list of what changed between OLD and NEW. Used to
    aggregate re-extraction effects in the bulk summary."""
    changes: List[str] = []
    if old_row is None:
        return ["new"]

    old_relevant = bool(old_row["is_relevant"])
    new_relevant = bool(new_event.get("is_relevant"))
    if old_relevant != new_relevant:
        changes.append(
            "now_relevant" if new_relevant else "now_irrelevant"
        )

    try:
        old_cats = set(json.loads(old_row["categories"] or "[]"))
    except json.JSONDecodeError:
        old_cats = set()
    new_cats = set(new_event.get("categories", []))

    dropped = old_cats - new_cats
    added = new_cats - old_cats
    if "institutional" in dropped:
        changes.append("dropped_institutional")
    if dropped - {"institutional"}:
        changes.append("dropped_categories")
    if added:
        changes.append("added_categories")

    if old_row["impact_magnitude"] != new_event.get("impact_magnitude"):
        changes.append("magnitude_changed")
    if old_row["impact_direction"] != new_event.get("impact_direction"):
        changes.append("direction_changed")

    if not changes:
        changes.append("unchanged")
    return changes


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    with db.connect(DB_PATH) as conn:
        if args.all_version is not None:
            article_ids = _fetch_article_ids_at_version(conn, args.all_version)
            if not article_ids:
                print(f"No articles whose latest event is at "
                      f"prompt_version={args.all_version!r}. Nothing to do.")
                return 0
            scope_label = f"--all-version={args.all_version}"
        else:
            article_ids = args.article_ids
            scope_label = f"articles={article_ids}"

        n = len(article_ids)
        mode = "COMMIT" if args.commit else "DRY-RUN"

        if (
            args.commit
            and args.all_version is not None
            and n > BULK_CONFIRM_THRESHOLD
            and not args.confirm
        ):
            print(
                f"Refusing to commit {n} re-extractions without --confirm. "
                f"Re-run with --commit --confirm if this is intended."
            )
            return 2

        print(f"reextract: {mode}  prompt_version={PROMPT_VERSION}  "
              f"{scope_label}  n={n}")
        print()

        client = GeminiClient()

        change_counts: dict = {}
        failures = 0

        for idx, aid in enumerate(article_ids, start=1):
            article_row = _fetch_article(conn, aid)
            print(f"=== [{idx} of {n}] aid={aid} ===")
            if article_row is None:
                print("  ERROR: article not found")
                print()
                failures += 1
                continue

            print(f"  source: {article_row['source_name']}")
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
                failures += 1
                continue

            print(_format_new(new_event))

            for change in _diff_summary(old_row, new_event):
                change_counts[change] = change_counts.get(change, 0) + 1

            if args.commit:
                eid = db.save_event(conn, aid, new_event)
                print(f"  COMMITTED as eid={eid}")
            else:
                print("  (dry-run; pass --commit to insert)")

            print()

        print("=" * 60)
        print(f"reextract summary  mode={mode}  n={n}  failures={failures}")
        for change, count in sorted(change_counts.items()):
            print(f"  {change:<24} {count:>4}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
