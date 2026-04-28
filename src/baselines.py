"""Loader for config/baselines.yaml.

Kept tiny on purpose. Scoring code passes the loaded dict around;
there's no class, no normalization beyond YAML parsing. If the file
shape changes, change both this and the consumers in src/score.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

BASELINES_PATH = Path(__file__).resolve().parent.parent / "config" / "baselines.yaml"


def load_baselines(path: Optional[Path] = None) -> dict:
    """Return the parsed baselines config as a dict.

    Top-level keys:
      - categories: dict of {category_name: {baseline, band_size, weight}}
      - term_start_date: ISO date string
      - baseline_changelog: list of {date, rationale} entries
    """
    p = path or BASELINES_PATH
    with p.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}
