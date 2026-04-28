"""Daily summary report. Read-only.

Usage:
    python -m src.daily_summary               # last 24 hours
    python -m src.daily_summary --days 7      # last 7 days
    python -m src.daily_summary --source fox  # case-insensitive substring

Prints recent extracted events grouped by category for spot-checking
pipeline output. Events affecting multiple categories are cross-listed
under each — this double-counts visually, but per-category counts are
the right granularity for the user's purposes. The footer total is
unique events; a note flags the cross-listing.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from src import db

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "pressy.db"

CATEGORY_ORDER = (
    "economy", "jobs", "housing", "health", "education",
    "science", "international", "constitutional", "moral", "institutional",
)

SUMMARY_MAX_CHARS = 150


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m src.daily_summary",
        description="Print recent extracted events grouped by category.",
    )
    p.add_argument(
        "--days", type=int, default=1,
        help="Look back N days (default: 1, i.e. last 24 hours).",
    )
    p.add_argument(
        "--source", type=str, default=None,
        help="Filter to a source by case-insensitive partial name match.",
    )
    return p.parse_args(argv)


def truncate(text: Optional[str], max_chars: int = SUMMARY_MAX_CHARS) -> str:
    if not text:
        return ""
    s = text.strip()
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 3].rstrip() + "..."


def parse_categories(raw: Optional[str]) -> list:
    """Categories are stored as JSON text. Tolerate corruption gracefully."""
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def window_label(days: int, start: datetime, end: datetime) -> str:
    head = "last 24 hours" if days == 1 else f"last {days} days"
    return f"{head} ({start.strftime('%Y-%m-%d')} to {end.strftime('%Y-%m-%d')})"


def render_event(row, current_category: Optional[str]) -> List[str]:
    """Lines for one event under one section. `current_category` is the
    category we're currently displaying under (None for UNCLASSIFIED);
    used to compute the cross-listing note."""
    if row["is_relevant"]:
        tag = f"[{row['impact_magnitude']} {row['impact_direction']}]"
    else:
        tag = "[—]"

    lines = [f"{tag} {row['event_title']}"]

    if current_category is not None:
        cats = parse_categories(row["categories"])
        others = [c for c in cats if c != current_category]
        if others:
            lines.append(f"    (also: {', '.join(others)})")

    lines.append(f"    {row['source_name']} | confidence: {row['confidence']}")
    summary = truncate(row["neutral_summary"])
    if summary:
        lines.append(f"    {summary}")
    return lines


def group_relevant_by_category(rows) -> dict:
    """Returns {category: [rows]} for is_relevant=true events. Each event
    appears under each of its categories. Within a category, rows are
    sorted by impact_magnitude DESC; ties resolve by extracted_at DESC,
    inherited from the SQL ORDER BY (Python sort is stable)."""
    grouped: dict = {}
    for row in rows:
        if not row["is_relevant"]:
            continue
        for cat in parse_categories(row["categories"]):
            grouped.setdefault(cat, []).append(row)

    for cat in grouped:
        grouped[cat].sort(
            key=lambda r: r["impact_magnitude"] or 0,
            reverse=True,
        )
    return grouped


def render_section(header: str, events: list, current_category: Optional[str], out) -> None:
    print(f"── {header} ──", file=out)
    for ev in events:
        for line in render_event(ev, current_category):
            print(line, file=out)
        print("", file=out)


def render_footer(rows, out) -> None:
    print("── SUMMARY ──", file=out)
    print(f"Total events: {len(rows)}", file=out)

    direction_counts = Counter(r["impact_direction"] for r in rows)
    print(
        "By direction: "
        f"{direction_counts.get('positive', 0)} positive, "
        f"{direction_counts.get('neutral', 0)} neutral, "
        f"{direction_counts.get('negative', 0)} negative",
        file=out,
    )

    mag_counts = Counter(r["impact_magnitude"] for r in rows)
    parts = [f"{mag_counts.get(m, 0)} mag-{m}" for m in (5, 4, 3, 2, 1)]
    print(f"By magnitude: {', '.join(parts)}", file=out)

    source_counts = Counter(r["source_name"] for r in rows)
    src_str = ", ".join(f"{name} ({n})" for name, n in source_counts.most_common())
    print(f"By source: {src_str}", file=out)

    if rows:
        avg = sum((r["impact_magnitude"] or 0) for r in rows) / len(rows)
        print(f"Average magnitude: {avg:.1f}", file=out)

    print("", file=out)
    print(
        "Note: events affecting multiple categories are listed in each, "
        "so per-category counts may exceed total.",
        file=out,
    )


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)

    with db.connect(DB_PATH) as conn:
        # Latest event per article only — re-extractions overwrite the
        # display, older rows stay in the DB as audit trail.
        rows = db.get_latest_event_per_article(
            conn, since_datetime=start, source_filter=args.source
        )

    out = sys.stdout

    if not rows:
        unit = "day" if args.days == 1 else "days"
        suffix = f" matching '{args.source}'" if args.source else ""
        print(f"No events extracted in the last {args.days} {unit}{suffix}.", file=out)
        return 0

    sources_in_window = {r["source_name"] for r in rows}
    print("Pressy daily summary", file=out)
    print(f"Window: {window_label(args.days, start, end)}", file=out)
    if args.source:
        print(f"Source filter: {args.source}", file=out)
    print(
        f"Events: {len(rows)} total across {len(sources_in_window)} "
        f"source{'s' if len(sources_in_window) != 1 else ''}",
        file=out,
    )
    print("", file=out)

    grouped = group_relevant_by_category(rows)
    for cat in CATEGORY_ORDER:
        events = grouped.get(cat, [])
        if not events:
            continue
        plural = "event" if len(events) == 1 else "events"
        render_section(f"{cat.upper()} ({len(events)} {plural})", events, cat, out)

    unclassified = [r for r in rows if not r["is_relevant"]]
    if unclassified:
        render_section(
            "UNCLASSIFIED (events with is_relevant=false)",
            unclassified, None, out,
        )

    render_footer(rows, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
