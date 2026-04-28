"""RSS ingestion for Pressy.

Reads sources from config/sources.yaml, pulls each feed, fetches article
bodies via trafilatura, applies a coarse relevance filter on titles,
deduplicates by URL and by content hash, and persists new articles.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from pathlib import Path
from typing import List, Optional

import feedparser
import trafilatura
import yaml

from src import db

log = logging.getLogger(__name__)

SOURCES_PATH = Path(__file__).resolve().parent.parent / "config" / "sources.yaml"

# Broad first-pass relevance filter on article titles. Tighten later
# once we see what actually flows through. Anything an article about
# US presidential administration action would plausibly mention.
RELEVANCE_KEYWORDS = (
    # Branches and people
    "president", "presidential", "administration", "white house",
    "congress", "senate", "speaker",
    "supreme court", "scotus", "federal court", "circuit court",
    "cabinet", "secretary",
    # Departments / agencies
    "treasury", "pentagon", "department of defense", "state department",
    "justice department", "doj", "fbi", "cia",
    "homeland security", "dhs", "ice",
    "epa", "fda", "irs", "sec", "cdc", "nih",
    # Current top officials (surnames; broad enough to catch generally)
    "trump", "vance", "rubio", "bondi", "hegseth", "kennedy", "noem",
    # Action vocabulary
    "executive order", "veto", "impeach", "tariff", "sanction",
    "federal", "agency",
)

MIN_BODY_CHARS = 200


def load_sources(path: Optional[Path] = None) -> List[dict]:
    p = path or SOURCES_PATH
    with p.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("sources", []) or []


def fetch_feed(source: dict) -> list:
    """Return feedparser entries for the source. Empty list on failure."""
    parsed = feedparser.parse(source["rss"])
    if parsed.bozo and not parsed.entries:
        log.warning(
            "feed empty or malformed for %s: %s",
            source.get("name"), parsed.bozo_exception,
        )
        return []
    return list(parsed.entries)


def fetch_article_body(url: str) -> Optional[str]:
    """Pull the article HTML and extract main text. None if either step fails."""
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        return None
    return trafilatura.extract(downloaded)


def compute_content_hash(body: str) -> str:
    """Stable SHA256 over whitespace-normalized lowercased body. Catches
    syndicated copies that differ only in whitespace or capitalization."""
    normalized = re.sub(r"\s+", " ", body.strip()).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def is_likely_relevant(title: str) -> bool:
    if not title:
        return False
    t = title.lower()
    return any(kw in t for kw in RELEVANCE_KEYWORDS)


def ingest_source(
    source: dict,
    conn: sqlite3.Connection,
    max_articles: int = 20,
) -> List[int]:
    """Fetch the feed, persist new articles, return list of new article ids."""
    source_id = db.get_or_create_source(
        conn,
        name=source["name"],
        rss=source["rss"],
        bias=source["bias"],
        bias_source=source.get("bias_source", "AllSides"),
    )

    new_ids: List[int] = []
    entries = fetch_feed(source)

    for entry in entries[:max_articles]:
        title = entry.get("title", "") or ""
        url = entry.get("link") or ""
        if not url:
            continue
        if not is_likely_relevant(title):
            continue

        body = fetch_article_body(url)
        if body is None:
            log.warning(
                "fetch_article_body failed for %s: %s",
                source.get("name"), url,
            )
            continue
        if len(body) < MIN_BODY_CHARS:
            continue

        h = compute_content_hash(body)
        if db.article_already_processed(conn, h):
            continue

        published = entry.get("published") or entry.get("updated")
        article_id = db.save_article(
            conn,
            source_id=source_id,
            url=url,
            title=title,
            body=body,
            published_date=published,
            content_hash=h,
        )
        new_ids.append(article_id)

    return new_ids


def ingest_all(conn: sqlite3.Connection) -> List[int]:
    """Ingest every source listed in sources.yaml. Per-source failures are
    logged but do not abort the run."""
    all_new: List[int] = []
    for source in load_sources():
        try:
            new_ids = ingest_source(source, conn)
            log.info("ingested %d new articles from %s", len(new_ids), source.get("name"))
            all_new.extend(new_ids)
        except Exception as e:
            log.warning("ingestion failed for %s: %s", source.get("name"), e)
    return all_new
