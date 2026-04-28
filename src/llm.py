"""Gemini client for Pressy.

Uses the google-genai SDK (the older google-generativeai package was
deprecated by Google in 2024). Pressy commits to Gemini for now; if we
ever switch providers we'll do a hard cutover here, not maintain
abstractions.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

log = logging.getLogger(__name__)

# Backoff schedule for 503 retries (seconds before retry 2 and retry 3).
# Three total attempts: initial + 2 retries.
RETRY_BACKOFFS = (5, 15)


class GeminiClientError(RuntimeError):
    """Raised when the Gemini client fails to return valid JSON after retries."""


class GeminiClient:
    """Thin wrapper around the google-genai SDK that returns parsed JSON.

    Two layers of retry:
    - 503 retry (outer): up to 3 total attempts on transient "high
      demand" errors from Gemini Flash. Backoff 5s then 15s. Non-503
      errors are not retried — they indicate real problems.
    - JSON-parse retry (inner): if the model returns non-JSON, one
      retry with a stricter "return only JSON" suffix. Lives inside
      each 503 attempt, so a 503 during a JSON-retry's second call
      counts as that whole attempt failing.

    A per-instance rate limit between call STARTS keeps us from
    burning quota. If a call takes 8s the next one fires immediately.
    """

    DEFAULT_MODEL = "gemini-2.5-flash"
    STRICT_SUFFIX = "\n\nReturn ONLY valid JSON, no other text."

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        api_key: Optional[str] = None,
        min_call_interval_seconds: float = 5.0,
    ):
        key = api_key or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise GeminiClientError(
                "GEMINI_API_KEY is not set. Copy .env.example to .env and add your key."
            )
        self._client = genai.Client(api_key=key)
        self._model_name = model
        self._config = types.GenerateContentConfig(
            response_mime_type="application/json",
        )
        self.min_call_interval = min_call_interval_seconds
        self._last_call_time: float = 0.0

    def _wait_for_rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_call_time
        if elapsed < self.min_call_interval:
            time.sleep(self.min_call_interval - elapsed)

    @staticmethod
    def _is_503_error(exc: BaseException) -> bool:
        """True if the exception looks like a Gemini 503 'high demand' error.

        google-genai surfaces API errors with a `.code` attribute (HTTP
        status as int). Older or differently-wrapped errors may only
        carry the code in the message string, so check both.
        """
        code = getattr(exc, "code", None)
        if code == 503:
            return True
        msg = str(exc).lower()
        if "503" not in msg:
            return False
        return any(
            marker in msg
            for marker in ("unavailable", "overloaded", "high demand")
        )

    def extract_json(self, prompt: str) -> dict:
        last_exc: Optional[BaseException] = None
        for attempt_idx in range(3):  # 0, 1, 2 → attempts 1/3, 2/3, 3/3
            try:
                return self._call_with_json_retry(prompt)
            except Exception as e:
                if not self._is_503_error(e):
                    raise
                last_exc = e
                if attempt_idx < 2:
                    wait = RETRY_BACKOFFS[attempt_idx]
                    next_attempt = attempt_idx + 2
                    log.info(
                        "503 from Gemini, retrying in %ds (attempt %d/3)",
                        wait, next_attempt,
                    )
                    time.sleep(wait)

        raise GeminiClientError(
            f"Gemini returned 503 on all 3 attempts: {last_exc}"
        ) from last_exc

    def _call_with_json_retry(self, prompt: str) -> dict:
        """One 503-attempt's worth of work: a call, and on JSON-parse
        failure, one stricter retry. Bubbles up 503s for the outer loop."""
        try:
            return self._call_and_parse(prompt)
        except (json.JSONDecodeError, ValueError):
            try:
                return self._call_and_parse(prompt + self.STRICT_SUFFIX)
            except (json.JSONDecodeError, ValueError) as e:
                raise GeminiClientError(
                    f"Gemini returned non-JSON output on both attempts: {e}"
                ) from e

    def _call_and_parse(self, prompt: str) -> dict:
        self._wait_for_rate_limit()
        self._last_call_time = time.monotonic()
        response = self._client.models.generate_content(
            model=self._model_name,
            contents=prompt,
            config=self._config,
        )
        text = (response.text or "").strip()
        if not text:
            raise ValueError("Gemini returned empty response")
        parsed = json.loads(text)
        return self._coerce_to_event_dict(parsed)

    @staticmethod
    def _coerce_to_event_dict(parsed) -> dict:
        """Validate that the parsed JSON is an object (or salvage a
        single-object list).

        The schema asks for a JSON object. Gemini almost always
        complies, but in rare cases it wraps the object in a list (the
        WHCD-arraignment article in the v2->v3 backfill triggered this:
        the model emitted `[ {...event...} ]` and downstream code's
        `event['source'] = ...` raised "list indices must be integers".)

        Tolerate single-element lists by unwrapping; raise a clear
        ValueError otherwise so the JSON-retry path with the strict
        suffix can take a second swing.
        """
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            if parsed and isinstance(parsed[0], dict):
                log.warning(
                    "Gemini returned a JSON list of %d element(s); "
                    "using the first object as the event",
                    len(parsed),
                )
                return parsed[0]
            inner = (
                "empty list" if not parsed
                else f"list of {type(parsed[0]).__name__}"
            )
            raise ValueError(
                f"Gemini returned a JSON {inner}; expected an object"
            )
        raise ValueError(
            f"Gemini returned JSON of type {type(parsed).__name__}; "
            f"expected an object"
        )
