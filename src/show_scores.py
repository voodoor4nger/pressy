"""Display Pressy administration scores.

Usage:
    python -m src.show_scores
    python -m src.show_scores --as-of 2026-04-27
    python -m src.show_scores --verbose

Reads from the events table and renders three sub-scores per category:
- Framing (tier='framing'): events extracted from news articles
- Action  (tier='action'):  events extracted from primary-source
                            government documents (Federal Register)
- Blend:  provisional 50/50 average. Treated as a placeholder while we
          accumulate enough action-tier data to calibrate a real blend.
          See docs/scoring.md (composite section) — once the analyst
          decides on weighting, update both the spec and the blend
          calculation here.
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

# Provisional blend weights — 50/50 framing/action. Update once the
# analyst calibrates real weights based on observed action data.
BLEND_WEIGHTS = {"framing": 0.5, "action": 0.5}


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
    """Look up titles, sources, dates, and tier for a list of event IDs."""
    if not event_ids:
        return {}
    placeholders = ",".join("?" * len(event_ids))
    sql = f"""
        SELECT e.id, e.event_title, e.tier,
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
    if places == 0:
        n = int(round(x))
        return f"{n:+d}"
    return f"{x:+.{places}f}"


def _format_event_date(s: str) -> str:
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


def _category_blend(framing_score: float, action_score: float) -> float:
    """Provisional 50/50 weighted average of framing and action scores."""
    wf = BLEND_WEIGHTS["framing"]
    wa = BLEND_WEIGHTS["action"]
    return (framing_score * wf + action_score * wa) / (wf + wa)


def render_summary(framing_result, action_result, out=sys.stdout) -> None:
    """Side-by-side framing / action / blend summary table."""
    print("Pressy administration scores", file=out)
    print(
        f"As of: {framing_result.as_of_date} "
        f"({framing_result.months_into_term:.0f} months into term, "
        f"k-factor: {framing_result.k_factor:.2f})",
        file=out,
    )
    print(
        f"Events in window: {framing_result.event_count_in_window} framing, "
        f"{action_result.event_count_in_window} action",
        file=out,
    )
    print(
        f"Blend: {int(BLEND_WEIGHTS['framing']*100)}% framing / "
        f"{int(BLEND_WEIGHTS['action']*100)}% action (provisional)",
        file=out,
    )
    print("", file=out)

    header = (
        f"{'Category':<{CATEGORY_COL_WIDTH}}"
        f"{'Baseline':>10}"
        f"{'Framing':>10}"
        f"{'Action':>10}"
        f"{'Blend':>10}"
        f"   {'Outlook (framing)'}"
    )
    print(header, file=out)
    print("─" * (CATEGORY_COL_WIDTH + 10 + 10 + 10 + 10 + 22), file=out)

    for cat in CATEGORY_ORDER:
        f = framing_result.category_scores.get(cat)
        a = action_result.category_scores.get(cat)
        if f is None and a is None:
            continue
        # Both per-tier results are computed against the same baseline,
        # so picking either is fine.
        baseline = (f or a)["baseline"]
        framing_score = (f or a)["score"] if f is None else f["score"]
        action_score = (a or f)["score"] if a is None else a["score"]
        blend_score = _category_blend(framing_score, action_score)
        outlook = (f or a)["outlook"]

        label = CATEGORY_DISPLAY[cat]
        print(
            f"{label:<{CATEGORY_COL_WIDTH}}"
            f"{baseline:>10}"
            f"{int(round(framing_score)):>10}"
            f"{int(round(action_score)):>10}"
            f"{int(round(blend_score)):>10}"
            f"   {outlook}",
            file=out,
        )

    print("", file=out)
    framing_composite = int(round(framing_result.composite))
    action_composite = int(round(action_result.composite))
    blend_composite = int(round(_category_blend(
        framing_result.composite, action_result.composite,
    )))
    print(
        f"Composite — framing: {framing_composite}  "
        f"action: {action_composite}  "
        f"blend: {blend_composite}  "
        f"({framing_result.composite_outlook} framing outlook)",
        file=out,
    )


def render_audit(framing_result, action_result, out=sys.stdout) -> None:
    """Per-category contribution detail across both tiers.

    Contributing events from both tiers are merged per category,
    sorted by absolute weighted impact, and labeled with their tier."""
    all_ids = sorted({
        eid
        for result in (framing_result, action_result)
        for evs in result.audit_trail.values()
        for (eid, _w) in evs
    })
    meta = _load_event_meta(all_ids)

    print("", file=out)
    print("── AUDIT TRAIL ──", file=out)
    for cat in CATEGORY_ORDER:
        f = framing_result.category_scores.get(cat)
        a = action_result.category_scores.get(cat)
        if f is None and a is None:
            continue
        label = CATEGORY_DISPLAY[cat]
        framing_score = f["score"] if f else (a["baseline"] if a else 0)
        action_score = a["score"] if a else (f["baseline"] if f else 0)
        blend_score = _category_blend(framing_score, action_score)

        print(
            f"\n{label} "
            f"(framing {int(round(framing_score))}, "
            f"action {int(round(action_score))}, "
            f"blend {int(round(blend_score))})",
            file=out,
        )

        contrib: list = []
        if f:
            contrib.extend(f["contributing_events"])
        if a:
            contrib.extend(a["contributing_events"])

        if not contrib:
            print("  (no contributing events)", file=out)
            continue

        contrib_sorted = sorted(contrib, key=lambda t: abs(t[1]), reverse=True)
        print("  Contributing events:", file=out)
        for eid, weight in contrib_sorted:
            m = meta.get(eid)
            if m:
                tier = m["tier"] if "tier" in m.keys() else "framing"
                tier_label = f"[{tier}]".ljust(10)
                print(
                    f"    {weight:+6.2f}  {tier_label} {m['event_title']} "
                    f"({_format_event_date(m['event_date'])}, {m['source_name']})",
                    file=out,
                )
            else:
                print(f"    {weight:+6.2f}  (event {eid} not found)", file=out)


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

    framing_result = compute_scores(as_of_date=as_of, tier="framing")
    action_result = compute_scores(as_of_date=as_of, tier="action")

    render_summary(framing_result, action_result)
    if args.verbose:
        render_audit(framing_result, action_result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
