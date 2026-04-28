"""Pure-function tests for src.score.

No DB, no LLM, no I/O. All events are synthetic dicts. Should run in
well under 5 seconds.
"""

from datetime import date, timedelta

import pytest

from src.score import (
    K_FACTOR_DECAY_MONTHS,
    K_FACTOR_FLOOR,
    OUTLOOK_THRESHOLD,
    compute_category_score,
    compute_composite,
    compute_event_weight,
    compute_k_factor,
    compute_outlook,
)


AS_OF = date(2026, 4, 27)


def make_event(
    *,
    eid: int = 1,
    is_relevant: bool = True,
    direction: str = "negative",
    magnitude: int = 3,
    confidence: str = "high",
    categories=("constitutional",),
    age_days: int = 0,
):
    return {
        "id": eid,
        "is_relevant": is_relevant,
        "impact_direction": direction,
        "impact_magnitude": magnitude,
        "confidence": confidence,
        "categories": list(categories),
        "event_date": AS_OF - timedelta(days=age_days),
        "event_title": f"event {eid}",
        "source_name": "Test Source",
    }


# ---------- compute_event_weight edge cases -----------------------------

def test_compute_event_weight_neutral_returns_zero():
    e = make_event(direction="neutral")
    assert compute_event_weight(e, AS_OF) == 0.0


def test_compute_event_weight_irrelevant_returns_zero():
    e = make_event(is_relevant=False)
    assert compute_event_weight(e, AS_OF) == 0.0


def test_compute_event_weight_old_event_returns_zero():
    # 731 days old — past the hard cutoff
    e = make_event(age_days=731)
    assert compute_event_weight(e, AS_OF) == 0.0


def test_compute_event_weight_decay():
    """A 90-day-old event should weigh half what a same-day event does."""
    today = make_event(age_days=0)
    old = make_event(age_days=90)
    w_today = compute_event_weight(today, AS_OF)
    w_old = compute_event_weight(old, AS_OF)
    # Allow for tiny float jitter
    assert w_today != 0
    assert w_old == pytest.approx(w_today * 0.5, rel=1e-9)


# ---------- compute_k_factor --------------------------------------------

TERM_START = date(2025, 1, 20)


def test_k_factor_at_month_zero_is_one():
    assert compute_k_factor(TERM_START, TERM_START) == 1.0


def test_k_factor_at_month_24_is_floor():
    # ~24 months out (using 30.4 days/month convention).
    as_of = TERM_START + timedelta(days=int(K_FACTOR_DECAY_MONTHS * 30.4))
    k = compute_k_factor(TERM_START, as_of)
    assert k == pytest.approx(K_FACTOR_FLOOR, abs=0.01)


def test_k_factor_caps_at_floor_after_month_24():
    as_of = TERM_START + timedelta(days=365 * 5)  # five years
    assert compute_k_factor(TERM_START, as_of) == K_FACTOR_FLOOR


# ---------- compute_category_score --------------------------------------

def test_category_score_within_band():
    """A single small negative event moves the score by less than the band."""
    e = make_event(magnitude=2, direction="negative", categories=("economy",))
    result = compute_category_score(
        category="economy", baseline=52, band_size=15,
        events=[e], k_factor=1.0, as_of_date=AS_OF,
    )
    assert result["raw_deviation"] == pytest.approx(-2.0)  # mag 2 * -1 * 1.0 (high) * 1.0 (today)
    assert result["deviation"] == pytest.approx(-2.0)       # within band
    assert result["score"] == pytest.approx(50.0)


def test_category_score_clamped_at_band_ceiling():
    """Many large negatives should pin the deviation at -band_size."""
    events = [
        make_event(eid=i, magnitude=5, direction="negative", categories=("constitutional",))
        for i in range(10)   # 10 × 8 = 80 raw, way past band 20
    ]
    result = compute_category_score(
        category="constitutional", baseline=18, band_size=20,
        events=events, k_factor=1.0, as_of_date=AS_OF,
    )
    assert result["raw_deviation"] < -20    # exceeds band
    assert result["deviation"] == -20.0     # but clamped
    assert result["score"] == pytest.approx(0.0)  # 18 - 20 = -2 → clamped to 0


def test_category_score_clamped_to_zero_to_hundred():
    """Even with a positive baseline + huge positive deviation, score caps at 100."""
    events = [
        make_event(eid=i, magnitude=5, direction="positive", categories=("economy",))
        for i in range(20)
    ]
    result = compute_category_score(
        category="economy", baseline=95, band_size=15,
        events=events, k_factor=1.0, as_of_date=AS_OF,
    )
    # Baseline 95 + clamped +15 = 110 → must cap at 100
    assert result["score"] == 100.0


def test_multi_category_event_applies_full_impact_to_each():
    """An event in [economy, international] contributes its full weight to BOTH."""
    e = make_event(magnitude=4, direction="negative",
                   categories=("economy", "international"))
    weight = compute_event_weight(e, AS_OF)  # mag 4 * -1 * 1.0 * 1.0 = -5

    eco = compute_category_score(
        category="economy", baseline=50, band_size=15,
        events=[e], k_factor=1.0, as_of_date=AS_OF,
    )
    intl = compute_category_score(
        category="international", baseline=50, band_size=15,
        events=[e], k_factor=1.0, as_of_date=AS_OF,
    )
    assert eco["raw_deviation"] == pytest.approx(weight)
    assert intl["raw_deviation"] == pytest.approx(weight)


def test_low_confidence_event_reduced_weight():
    high = make_event(confidence="high")
    low = make_event(confidence="low")
    w_high = compute_event_weight(high, AS_OF)
    w_low = compute_event_weight(low, AS_OF)
    assert w_low == pytest.approx(w_high * 0.4)


# ---------- compute_outlook ---------------------------------------------

def test_outlook_recent_more_negative_than_historical_returns_negative():
    """Big recent dump of negative events vs. a stable historical period."""
    events = [
        make_event(eid=1, magnitude=5, direction="negative", age_days=5),
        make_event(eid=2, magnitude=5, direction="negative", age_days=10),
        make_event(eid=3, magnitude=2, direction="positive", age_days=60),
    ]
    assert compute_outlook(events, k_factor=1.0, as_of_date=AS_OF) == "negative"


def test_outlook_within_threshold_returns_stable():
    """Recent and historical are similar — delta below threshold."""
    events = [
        make_event(eid=1, magnitude=2, direction="negative", age_days=5),
        make_event(eid=2, magnitude=2, direction="negative", age_days=60),
    ]
    # Recent ≈ -2, historical ≈ -2 * decay(60d) ≈ -1.26 → delta ≈ -0.74 (< threshold 2)
    assert compute_outlook(events, k_factor=1.0, as_of_date=AS_OF) == "stable"


# ---------- compute_composite -------------------------------------------

def _trivial_score(value: float) -> dict:
    return {
        "score": value,
        "baseline": 0,
        "band_size": 0,
        "deviation": 0,
        "raw_deviation": 0,
        "contributing_events": [],
        "outlook": "stable",
    }


def test_composite_default_equal_weights():
    cats = {
        "economy": _trivial_score(50),
        "jobs":    _trivial_score(60),
        "housing": _trivial_score(40),
    }
    weights = {"economy": 1.0, "jobs": 1.0, "housing": 1.0}
    assert compute_composite(cats, weights) == pytest.approx(50.0)


def test_composite_with_custom_weights():
    cats = {
        "economy": _trivial_score(50),
        "jobs":    _trivial_score(60),
        "housing": _trivial_score(40),
    }
    weights = {"economy": 2.0, "jobs": 1.0, "housing": 0.0}
    # (50*2 + 60*1) / (2+1) = 160 / 3 = 53.333...
    assert compute_composite(cats, weights) == pytest.approx(160 / 3)
