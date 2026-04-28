"""Display Pressy administration scores.

Usage:
    python -m src.show_scores
    python -m src.show_scores --as-of 2026-04-27
    python -m src.show_scores --verbose

Read-only against the events table.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

from src import db
from src.score import DB_PATH, compute_scores

# Display order matches docs/scoring.md and the daily_summary CLI.
CATEGORY_ORDER = (
    "economy", "jobs", "housing", "health", "education",
    "science", "international", "constitutional", "moral", "institutional",
)

CATEGORY_DISPLAY = {
    "economy": "Economy",
    "jobs": "Job market",
    "housing": "Housing",
    "health": "Health",
    "education": "Education",
    "science": "Science & technology",
    "international": "International",
    "constitutional": "Constitutional",
    "moral": "Moral leadership",
    "institutional": "Institutional",
}

CATEGORY_COL_WIDTH = 22


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m src.show_scores",
        description="Display Pressy administration scores.",
    )
    p.add_argument(
        "--as-of", type=str, default=None,
        help="As-of date in YYYY-MM-DD. Default: today.",
    )
    p.add_argument(
        "--verbose", action="store_true",
        help="Print per-category audit trail.",
    )
    return p.parse_args(argv)


def _load_event_meta(event_ids: List[int]) -> dict:
    """Look up titles, sources, and dates for a list of event IDs."""
    if not event_ids:
        return {}
    placeholders = ",".join("?" * len(event_ids))
    sql = f"""
        SELECT e.id, e.event_title,
               COALESCE(a.published_date, e.extracted_at) AS event_date,
               s.name AS source_name
        FROM events e
        JOIN articles a ON a.id = e.article_id
        JOIN sources  s ON s.id = a.source_id
        WHERE e.id IN ({placeholders})
    """
    with db.connect(DB_PATH) as conn:
        rows = conn.execute(sql, event_ids).fetchall()
    return {r["id"]: r for r in rows}


def _format_signed(x: float, places: int = 0) -> str:
    """Signed integer or float, with sign always present."""
    if places == 0:
        n = int(round(x))
        return f"{n:+d}"
    return f"{x:+.{places}f}"


def _format_event_date(s: str) -> str:
    """Trim a stored date to YYYY-MM-DD if we can; else pass through."""
    if not s:
        return ""
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date().isoformat()
    except (ValueError, TypeError):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
                    "%a, %d %b %Y %H:%M:%S %z",
                    "%a, %d %b %Y %H:%M:%S %Z",
                    "%a, %d %b %Y %H:%M:%S GMT"):
            try:
                return datetime.strptime(s.strip(), fmt).date().isoformat()
            except (ValueError, TypeError):
                pass
    return s[:10]


def render_summary(result, out=sys.stdout) -> None:
    print("Pressy administration scores", file=out)
    print(
        f"As of: {result.as_of_date} "
        f"({result.months_into_term:.0f} months into term, "
        f"k-factor: {result.k_factor:.2f})",
        file=out,
    )
    print(f"Events in window: {result.event_count_in_window}", file=out)
    print("", file=out)

    header = (
        f"{'Category':<{CATEGORY_COL_WIDTH}}"
        f"{'Baseline':>10}"
        f"{'Score':>8}"
        f"{'Deviation':>12}"
        f"   {'Outlook'}"
    )
    print(header, file=out)
    print("─" * (CATEGORY_COL_WIDTH + 10 + 8 + 12 + 13), file=out)

    for cat in CATEGORY_ORDER:
        s = result.category_scores.get(cat)
        if s is None:
            continue
        label = CATEGORY_DISPLAY[cat]
        print(
            f"{label:<{CATEGORY_COL_WIDTH}}"
            f"{s['baseline']:>10}"
            f"{int(round(s['score'])):>8}"
            f"{_format_signed(s['deviation']):>12}"
            f"   {s['outlook']}",
            file=out,
        )

    print("", file=out)
    print(
        f"Composite: {int(round(result.composite))} / "
        f"{result.composite_outlook} outlook",
        file=out,
    )


def render_audit(result, out=sys.stdout) -> None:
    """Per-category contribution detail."""
    # Collect all event ids referenced across categories so we can
    # batch-load metadata (title, source, date).
    all_ids = sorted({eid for evs in result.audit_trail.values() for (eid, _w) in evs})
    meta = _load_event_meta(all_ids)

    print("", file=out)
    print("── AUDIT TRAIL ──", file=out)
    for cat in CATEGORY_ORDER:
        s = result.category_scores.get(cat)
        if s is None:
            continue
        label = CATEGORY_DISPLAY[cat]
        print(
            f"\n{label} (baseline {s['baseline']}, "
            f"deviation {_format_signed(s['deviation'])}, "
            f"score {int(round(s['score']))}, "
            f"{s['outlook']} outlook)",
            file=out,
        )
        contrib = s["contributing_events"]
        if not contrib:
            print("  (no contributing events)", file=out)
            continue

        # Sort by absolute weighted impact, descending — biggest movers first.
        contrib_sorted = sorted(contrib, key=lambda t: abs(t[1]), reverse=True)

        print("  Contributing events:", file=out)
        for eid, weight in contrib_sorted:
            m = meta.get(eid)
            if m:
                print(
                    f"    {weight:+6.2f}  {m['event_title']} "
                    f"({_format_event_date(m['event_date'])}, {m['source_name']})",
                    file=out,
                )
            else:
                print(f"    {weight:+6.2f}  (event {eid} not found)", file=out)

        total_weighted = sum(w for (_eid, w) in contrib)
        post_k = total_weighted * result.k_factor
        print(f"  Total weighted: {total_weighted:+.2f}", file=out)
        print(
            f"  After k-factor ({result.k_factor:.2f}): {post_k:+.2f}",
            file=out,
        )
        if abs(s["raw_deviation"]) > s["band_size"]:
            print(
                f"  NOTE: raw deviation {s['raw_deviation']:+.2f} exceeds "
                f"band ±{s['band_size']} — clamped.",
                file=out,
            )


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    as_of = None
    if args.as_of:
        try:
            as_of = date.fromisoformat(args.as_of)
        except ValueError:
            print(f"error: --as-of must be YYYY-MM-DD, got {args.as_of!r}",
                  file=sys.stderr)
            return 2

    result = compute_scores(as_of_date=as_of)

    render_summary(result)
    if args.verbose:
        render_audit(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
