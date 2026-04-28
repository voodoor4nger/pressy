"""SQLite persistence for Pressy.

Personal-scale storage. No ORM — just sqlite3 + helpers. Connections are
opened in autocommit mode (isolation_level=None) so each helper writes
immediately; the pipeline can crash mid-run without losing the articles
already saved.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, List, Optional, Union


SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    rss_url TEXT NOT NULL,
    bias TEXT NOT NULL,
    bias_source TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    published_date TEXT,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    content_hash TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_articles_content_hash ON articles(content_hash);
CREATE INDEX IF NOT EXISTS idx_articles_source        ON articles(source_id);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER NOT NULL REFERENCES articles(id),
    event_title TEXT NOT NULL,
    categories TEXT NOT NULL,                       -- JSON list
    impact_direction TEXT NOT NULL,
    impact_magnitude INTEGER NOT NULL,
    neutral_summary TEXT NOT NULL,
    framing_indicators TEXT NOT NULL,               -- JSON object
    confidence TEXT NOT NULL,
    is_relevant INTEGER NOT NULL,                   -- 0/1
    extracted_at TEXT NOT NULL DEFAULT (datetime('now')),
    prompt_version TEXT NOT NULL DEFAULT 'v2'       -- 'v2' is the migration backfill
);

CREATE INDEX IF NOT EXISTS idx_events_article ON events(article_id);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    articles_fetched INTEGER DEFAULT 0,
    articles_extracted INTEGER DEFAULT 0,
    errors_count INTEGER DEFAULT 0
);
"""


def init_db(path: Union[str, Path]) -> sqlite3.Connection:
    """Open (creating if needed) the database at `path`. Returns a connection
    with foreign keys enforced and rows accessible by column name. Runs
    idempotent schema migrations on every open."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply any column additions to existing DBs. Idempotent — safe to
    call on a fresh DB (the columns will already exist from CREATE TABLE)
    or on an old DB (the columns will be added with a backfill default)."""
    existing_event_cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(events)").fetchall()
    }
    if "prompt_version" not in existing_event_cols:
        # Existing rows backfilled to 'v2' — that's the prompt version
        # under which they were originally extracted.
        conn.execute(
            "ALTER TABLE events ADD COLUMN prompt_version TEXT NOT NULL DEFAULT 'v2'"
        )


@contextmanager
def connect(path: Union[str, Path]) -> Iterator[sqlite3.Connection]:
    """Context-managed connection: opens, yields, closes."""
    conn = init_db(path)
    try:
        yield conn
    finally:
        conn.close()


def get_or_create_source(
    conn: sqlite3.Connection,
    name: str,
    rss: str,
    bias: str,
    bias_source: str,
) -> int:
    row = conn.execute("SELECT id FROM sources WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO sources (name, rss_url, bias, bias_source) VALUES (?, ?, ?, ?)",
        (name, rss, bias, bias_source),
    )
    return cur.lastrowid


def save_article(
    conn: sqlite3.Connection,
    source_id: int,
    url: str,
    title: str,
    body: str,
    published_date: Optional[str],
    content_hash: str,
) -> int:
    """Insert and return new id, or return the existing id if the URL is
    already in the table."""
    row = conn.execute("SELECT id FROM articles WHERE url = ?", (url,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        """INSERT INTO articles
           (source_id, url, title, body, published_date, content_hash)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (source_id, url, title, body, published_date, content_hash),
    )
    return cur.lastrowid


def save_event(
    conn: sqlite3.Connection,
    article_id: int,
    event: dict,
) -> int:
    """Insert an event from extract_event() output. Categories and
    framing_indicators are serialized to JSON text. Always inserts —
    re-extractions append a new row and the older row is kept as audit
    trail. Use get_latest_event_per_article() to read only the newest
    event per article."""
    cur = conn.execute(
        """INSERT INTO events
           (article_id, event_title, categories, impact_direction,
            impact_magnitude, neutral_summary, framing_indicators,
            confidence, is_relevant, prompt_version)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            article_id,
            event["event_title"],
            json.dumps(event.get("categories", [])),
            event["impact_direction"],
            int(event["impact_magnitude"]),
            event["neutral_summary"],
            json.dumps(event.get("framing_indicators", {})),
            event["confidence"],
            1 if event.get("is_relevant") else 0,
            event.get("prompt_version", "unknown"),
        ),
    )
    return cur.lastrowid


def get_events_since(
    conn: sqlite3.Connection,
    since_datetime: datetime,
    source_filter: Optional[str] = None,
) -> List[sqlite3.Row]:
    """Return events extracted at or after `since_datetime`, joined with
    article and source data. Ordered by extracted_at DESC.

    `source_filter` does a case-insensitive substring match on the
    source name. SQLite's datetime('now') stores UTC as
    'YYYY-MM-DD HH:MM:SS', so we compare using the same string format —
    the caller should pass a UTC datetime.
    """
    since_str = since_datetime.strftime("%Y-%m-%d %H:%M:%S")

    sql = """
        SELECT
            e.id, e.event_title, e.categories, e.impact_direction,
            e.impact_magnitude, e.neutral_summary, e.framing_indicators,
            e.confidence, e.is_relevant, e.extracted_at,
            a.title AS article_title, a.url AS article_url,
            a.published_date,
            s.name AS source_name, s.bias
        FROM events e
        JOIN articles a ON a.id = e.article_id
        JOIN sources  s ON s.id = a.source_id
        WHERE e.extracted_at >= ?
    """
    params: list = [since_str]

    if source_filter:
        sql += " AND lower(s.name) LIKE ?"
        params.append(f"%{source_filter.lower()}%")

    sql += " ORDER BY e.extracted_at DESC"

    return conn.execute(sql, params).fetchall()


def get_latest_event_per_article(
    conn: sqlite3.Connection,
    since_datetime: Optional[datetime] = None,
    source_filter: Optional[str] = None,
) -> List[sqlite3.Row]:
    """Same shape as get_events_since, but returns at most one row per
    article — the most recently inserted event. Older rows are preserved
    in the table as audit trail.

    Latest is determined by MAX(events.id), which monotonically tracks
    insertion order under AUTOINCREMENT. This is more reliable than
    comparing extracted_at strings if two re-extractions happen in the
    same second.
    """
    sql = """
        SELECT
            e.id, e.event_title, e.categories, e.impact_direction,
            e.impact_magnitude, e.neutral_summary, e.framing_indicators,
            e.confidence, e.is_relevant, e.extracted_at, e.prompt_version,
            a.title AS article_title, a.url AS article_url,
            a.published_date,
            s.name AS source_name, s.bias
        FROM events e
        INNER JOIN (
            SELECT article_id, MAX(id) AS max_id
            FROM events
            GROUP BY article_id
        ) latest ON e.id = latest.max_id
        JOIN articles a ON a.id = e.article_id
        JOIN sources  s ON s.id = a.source_id
        WHERE 1=1
    """
    params: list = []

    if since_datetime is not None:
        sql += " AND e.extracted_at >= ?"
        params.append(since_datetime.strftime("%Y-%m-%d %H:%M:%S"))

    if source_filter:
        sql += " AND lower(s.name) LIKE ?"
        params.append(f"%{source_filter.lower()}%")

    sql += " ORDER BY e.extracted_at DESC"

    return conn.execute(sql, params).fetchall()


def article_already_processed(conn: sqlite3.Connection, content_hash: str) -> bool:
    """True if any article with this body hash has been ingested before.
    Catches republished or syndicated copies across sources."""
    row = conn.execute(
        "SELECT 1 FROM articles WHERE content_hash = ? LIMIT 1", (content_hash,)
    ).fetchone()
    return row is not None


def start_run(conn: sqlite3.Connection) -> int:
    cur = conn.execute("INSERT INTO runs DEFAULT VALUES")
    return cur.lastrowid


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    fetched: int,
    extracted: int,
    errors: int,
) -> None:
    conn.execute(
        """UPDATE runs
           SET finished_at = datetime('now'),
               articles_fetched = ?,
               articles_extracted = ?,
               errors_count = ?
           WHERE id = ?""",
        (fetched, extracted, errors, run_id),
    )
