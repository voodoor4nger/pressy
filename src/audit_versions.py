"""Audit the events table by prompt_version.

Usage:
    python -m src.audit_versions

Prints total event count, the per-version breakdown (count, oldest event
published date, newest event published date, most recent extraction
timestamp), and the count restricted to the latest event per article —
which is what re-extraction targets actually move.
"""

from __future__ import annotations

from pathlib import Path

from src import db
from src.extract import PROMPT_VERSION

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "pressy.db"


def main() -> int:
    with db.connect(DB_PATH) as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"]

        per_version = conn.execute(
            """
            SELECT
                e.prompt_version,
                COUNT(*)              AS n,
                MIN(a.published_date) AS oldest_pub,
                MAX(a.published_date) AS newest_pub,
                MAX(e.extracted_at)   AS last_extracted
            FROM events e
            JOIN articles a ON a.id = e.article_id
            GROUP BY e.prompt_version
            ORDER BY e.prompt_version
            """
        ).fetchall()

        latest_per_version = conn.execute(
            """
            SELECT prompt_version, COUNT(*) AS n
            FROM (
                SELECT e.prompt_version
                FROM events e
                INNER JOIN (
                    SELECT article_id, MAX(id) AS max_id
                    FROM events
                    GROUP BY article_id
                ) latest ON e.id = latest.max_id
            )
            GROUP BY prompt_version
            ORDER BY prompt_version
            """
        ).fetchall()

    print(f"Current PROMPT_VERSION (from prompts/extract_event.txt): {PROMPT_VERSION}")
    print(f"Total events in DB: {total}")
    print()
    print("All event rows by prompt_version (includes audit-trail rows):")
    print(f"  {'version':<10} {'count':>6}  {'oldest_pub':<11}  "
          f"{'newest_pub':<11}  {'last_extracted':<20}")
    for row in per_version:
        print(
            f"  {row['prompt_version']:<10} "
            f"{row['n']:>6}  "
            f"{(row['oldest_pub'] or '-'):<11}  "
            f"{(row['newest_pub'] or '-'):<11}  "
            f"{(row['last_extracted'] or '-'):<20}"
        )
    print()
    print("Latest event per article only (what scoring actually reads):")
    print(f"  {'version':<10} {'count':>6}")
    for row in latest_per_version:
        print(f"  {row['prompt_version']:<10} {row['n']:>6}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
