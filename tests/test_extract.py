"""Smoke test for src.extract.extract_event.

Skipped automatically if GEMINI_API_KEY is not set (so CI / a dry
checkout doesn't spuriously fail). When the key is present, this hits
the live Gemini API.
"""

import os

import pytest

from src.extract import extract_event

REQUIRED_KEYS = {
    "event_title",
    "categories",
    "impact_direction",
    "impact_magnitude",
    "neutral_summary",
    "framing_indicators",
    "confidence",
    "is_relevant",
}

VALID_DIRECTIONS = {"positive", "negative", "neutral"}


@pytest.mark.skipif(
    not os.environ.get("GEMINI_API_KEY"),
    reason="GEMINI_API_KEY not set; skipping live extraction test.",
)
def test_extract_event_returns_well_formed_event():
    article = {
        "source": "Reuters",
        "date": "2026-02-03",
        "title": "Treasury announces 10 percent tariff on Canadian lumber imports",
        "body": (
            "The U.S. Treasury Department announced on Monday a new 10 percent "
            "tariff on lumber imported from Canada, scheduled to take effect "
            "March 15. Treasury Secretary said the move was intended to support "
            "domestic timber producers. Canadian officials called the tariff "
            "unjustified and said they were considering reciprocal measures. "
            "Economists projected a modest increase in US homebuilding costs."
        ),
        "url": "https://reuters.com/example",
    }

    event = extract_event(article)

    missing = REQUIRED_KEYS - set(event.keys())
    assert not missing, f"missing keys in extracted event: {missing}"

    assert isinstance(event["categories"], list)
    assert 1 <= len(event["categories"]) <= 3, (
        f"categories must have 1-3 items, got {event['categories']}"
    )

    assert isinstance(event["impact_magnitude"], int)
    assert 1 <= event["impact_magnitude"] <= 5, (
        f"impact_magnitude out of range: {event['impact_magnitude']}"
    )

    assert event["impact_direction"] in VALID_DIRECTIONS, (
        f"unexpected impact_direction: {event['impact_direction']}"
    )
