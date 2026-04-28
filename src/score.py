"""Scoring system for Pressy.

Implements docs/scoring.md. Read-only against the events table; does
not modify the pipeline.

Two layers:
  - Pure functions (compute_event_weight, compute_k_factor,
    compute_category_score, compute_outlook, compute_composite).
    No I/O, no DB. Fully testable from synthetic event dicts.
  - Orchestration (compute_scores) that loads from DB + config and
    returns a structured ScoreResult.

Event dict shape expected by the pure functions:
  {
    "id": int,
    "is_relevant": bool,
    "impact_direction": "positive" | "negative" | "neutral",
    "impact_magnitude": int (1-5),
    "confidence": "high" | "medium" | "low",
    "categories": list[str],
    "event_date": date,                 # used for time decay
    "event_title": str,                 # for audit display
    "source_name": str,                 # for audit display
  }
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, List, Optional

from src import baselines as baselines_mod
from src import db


# ---------------------------------------------------------------------------
# Constants — see docs/scoring.md "Parameter summary"
# ---------------------------------------------------------------------------

MAGNITUDE_IMPACTS = {1: 1, 2: 2, 3: 3, 4: 5, 5: 8}   # Fibonacci-like (v1.1)
CONFIDENCE_WEIGHTS = {"high": 1.0, "medium": 0.7, "low": 0.4}
DIRECTION_SIGNS = {"positive": 1, "negative": -1, "neutral": 0}

HALF_LIFE_DAYS = 90
HARD_CUTOFF_DAYS = 730

K_FACTOR_FLOOR = 0.5
K_FACTOR_DECAY_MONTHS = 24

OUTLOOK_THRESHOLD = 2

# Approximate days-per-month used for converting age-in-days to months.
# The exact length doesn't matter as long as it's used consistently.
DAYS_PER_MONTH = 30.4

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "pressy.db"


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------

def _to_date(d) -> date:
    """Coerce a date/datetime to a date object."""
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    raise TypeError(f"expected date or datetime, got {type(d).__name__}")


def compute_event_weight(event: dict, as_of_date: date) -> float:
    """Signed weighted impact of one event.

    Returns 0 if the event is irrelevant, neutral, or older than the
    hard cutoff. Otherwise applies magnitude × direction × confidence
    × time-decay.
    """
    if not event.get("is_relevant"):
        return 0.0

    direction = event.get("impact_direction")
    sign = DIRECTION_SIGNS.get(direction, 0)
    if sign == 0:
        return 0.0

    magnitude = event.get("impact_magnitude")
    impact = MAGNITUDE_IMPACTS.get(magnitude)
    if impact is None:
        return 0.0

    confidence_weight = CONFIDENCE_WEIGHTS.get(event.get("confidence"), 0.0)
    if confidence_weight == 0.0:
        return 0.0

    age_days = (_to_date(as_of_date) - _to_date(event["event_date"])).days
    if age_days < 0:
        age_days = 0      # future-dated events: treat as today
    if age_days > HARD_CUTOFF_DAYS:
        return 0.0

    time_decay = 0.5 ** (age_days / HALF_LIFE_DAYS)
    return impact * sign * confidence_weight * time_decay


def compute_k_factor(term_start_date: date, as_of_date: date) -> float:
    """Linear decay from 1.0 at month 0 to K_FACTOR_FLOOR at month 24.
    Capped at the floor afterward."""
    days_into_term = (_to_date(as_of_date) - _to_date(term_start_date)).days
    if days_into_term < 0:
        return 1.0
    months = days_into_term / DAYS_PER_MONTH
    if months >= K_FACTOR_DECAY_MONTHS:
        return K_FACTOR_FLOOR
    return 1.0 - (months / K_FACTOR_DECAY_MONTHS) * (1.0 - K_FACTOR_FLOOR)


def _events_in_age_range(events: Iterable[dict], as_of_date: date,
                         min_days: int, max_days: int) -> List[dict]:
    out = []
    for e in events:
        ed = e.get("event_date")
        if ed is None:
            continue
        age = (_to_date(as_of_date) - _to_date(ed)).days
        if min_days <= age < max_days:
            out.append(e)
    return out


def compute_outlook(events: Iterable[dict], k_factor: float,
                    as_of_date: date) -> str:
    """Recent (0-30 days) vs historical (31-90 days) momentum.

    `k_factor` is accepted for API symmetry but cancels in the
    difference, so the threshold operates on raw delta.
    """
    events = list(events)
    recent = sum(
        compute_event_weight(e, as_of_date)
        for e in _events_in_age_range(events, as_of_date, 0, 30)
    )
    historical = sum(
        compute_event_weight(e, as_of_date)
        for e in _events_in_age_range(events, as_of_date, 30, 90)
    )
    delta = recent - historical
    if abs(delta) < OUTLOOK_THRESHOLD:
        return "stable"
    return "positive" if delta > 0 else "negative"


def compute_category_score(
    category: str,
    baseline: int,
    band_size: int,
    events: Iterable[dict],
    k_factor: float,
    as_of_date: date,
) -> dict:
    """Score for one category.

    Multi-category events apply their FULL impact to each category —
    the caller is expected to pass the events that include `category`
    in their categories list.
    """
    events = list(events)
    contributing: List[tuple] = []
    raw_sum = 0.0
    for e in events:
        w = compute_event_weight(e, as_of_date)
        if w == 0.0:
            continue
        raw_sum += w
        contributing.append((e.get("id"), w))

    raw_deviation = raw_sum * k_factor
    clamped_deviation = max(-band_size, min(band_size, raw_deviation))
    score = max(0, min(100, baseline + clamped_deviation))

    return {
        "category": category,
        "baseline": baseline,
        "band_size": band_size,
        "score": score,
        "deviation": clamped_deviation,
        "raw_deviation": raw_deviation,
        "contributing_events": contributing,
        "outlook": compute_outlook(events, k_factor, as_of_date),
    }


def compute_composite(category_scores: dict, weights: dict) -> float:
    """Weighted average of category scores. Categories not in `weights`
    get weight 0 (excluded). Returns 0.0 if all weights are zero."""
    total_weight = 0.0
    weighted_sum = 0.0
    for cat, score_dict in category_scores.items():
        w = weights.get(cat, 0.0)
        if w <= 0:
            continue
        total_weight += w
        weighted_sum += score_dict["score"] * w
    if total_weight == 0.0:
        return 0.0
    return weighted_sum / total_weight


def _composite_outlook_from_categories(category_scores: dict) -> str:
    """Simple majority of non-stable per-category outlooks."""
    pos = neg = 0
    for s in category_scores.values():
        if s["outlook"] == "positive":
            pos += 1
        elif s["outlook"] == "negative":
            neg += 1
    if pos > neg:
        return "positive"
    if neg > pos:
        return "negative"
    return "stable"


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

@dataclass
class ScoreResult:
    as_of_date: date
    term_start_date: date
    k_factor: float
    months_into_term: float
    event_count_in_window: int
    category_scores: dict
    composite: float
    composite_outlook: str
    audit_trail: dict = field(default_factory=dict)


def _parse_event_date(row) -> Optional[date]:
    """Best-effort extraction of an event date.

    Prefer `published_date` (when the news happened) but fall back to
    `extracted_at` (when we processed it). Published dates from RSS
    feeds vary wildly in format; we only commit to parsing a few
    common ones. If parsing fails, we use extracted_at.
    """
    pub = row["published_date"]
    if pub:
        d = _try_parse_date(pub)
        if d is not None:
            return d
    extracted = row["extracted_at"]
    if extracted:
        d = _try_parse_date(extracted)
        if d is not None:
            return d
    return None


def _try_parse_date(s: str) -> Optional[date]:
    s = s.strip()
    # SQLite extracted_at format
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d", "%a, %d %b %Y %H:%M:%S %z",
                "%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S GMT"):
        try:
            return datetime.strptime(s, fmt).date()
        except (ValueError, TypeError):
            pass
    # Try ISO 8601 with tz
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


def _row_to_event(row) -> dict:
    return {
        "id": row["id"],
        "is_relevant": bool(row["is_relevant"]),
        "impact_direction": row["impact_direction"],
        "impact_magnitude": row["impact_magnitude"],
        "confidence": row["confidence"],
        "categories": json.loads(row["categories"] or "[]"),
        "event_date": _parse_event_date(row),
        "event_title": row["event_title"],
        "source_name": row["source_name"],
    }


def _load_recent_events(conn, as_of_date: date) -> List[dict]:
    """Load all events within the hard cutoff, latest-per-article only."""
    cutoff = as_of_date - timedelta(days=HARD_CUTOFF_DAYS)
    rows = db.get_latest_event_per_article(conn, since_datetime=datetime.combine(cutoff, datetime.min.time()))
    out = []
    for r in rows:
        e = _row_to_event(r)
        if e["event_date"] is None:
            continue
        out.append(e)
    return out


def compute_scores(as_of_date: Optional[date] = None) -> ScoreResult:
    """End-to-end: load config, load events from DB, compute, return."""
    if as_of_date is None:
        as_of_date = date.today()
    as_of_date = _to_date(as_of_date)

    cfg = baselines_mod.load_baselines()
    cats_cfg = cfg["categories"]
    term_start_date = _to_date(datetime.fromisoformat(cfg["term_start_date"]))

    k_factor = compute_k_factor(term_start_date, as_of_date)
    months_into_term = (as_of_date - term_start_date).days / DAYS_PER_MONTH

    with db.connect(DB_PATH) as conn:
        events = _load_recent_events(conn, as_of_date)

    category_scores: dict = {}
    audit_trail: dict = {}
    for cat, cfg_entry in cats_cfg.items():
        cat_events = [e for e in events if cat in e["categories"]]
        result = compute_category_score(
            category=cat,
            baseline=cfg_entry["baseline"],
            band_size=cfg_entry["band_size"],
            events=cat_events,
            k_factor=k_factor,
            as_of_date=as_of_date,
        )
        category_scores[cat] = result
        audit_trail[cat] = result["contributing_events"]

    weights = {cat: cats_cfg[cat]["weight"] for cat in cats_cfg}
    composite = compute_composite(category_scores, weights)
    composite_outlook = _composite_outlook_from_categories(category_scores)

    return ScoreResult(
        as_of_date=as_of_date,
        term_start_date=term_start_date,
        k_factor=k_factor,
        months_into_term=months_into_term,
        event_count_in_window=len(events),
        category_scores=category_scores,
        composite=composite,
        composite_outlook=composite_outlook,
        audit_trail=audit_trail,
    )
