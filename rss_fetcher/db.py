"""
Database connection, schema bootstrap, and feed entry insertion.
"""
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Generator

import psycopg
from psycopg import Connection as PgConnection

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS feed_entries (
    id           BIGSERIAL PRIMARY KEY,
    feed_name    TEXT        NOT NULL,
    feed_url     TEXT        NOT NULL,
    guid         TEXT        NOT NULL,
    title            TEXT,
    original_title   TEXT,
    link             TEXT,
    summary          TEXT,
    original_summary TEXT,
    author           TEXT,
    geo_tags     TEXT[]      NOT NULL DEFAULT '{}',
    topic_tags   TEXT[]      NOT NULL DEFAULT '{}',
    gist         TEXT,
    published_at TIMESTAMPTZ,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_feed_entry UNIQUE (feed_url, guid)
);

-- Idempotent migrations
ALTER TABLE feed_entries ADD COLUMN IF NOT EXISTS geo_tags          TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE feed_entries ADD COLUMN IF NOT EXISTS topic_tags        TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE feed_entries ADD COLUMN IF NOT EXISTS gist              TEXT;
ALTER TABLE feed_entries ADD COLUMN IF NOT EXISTS original_title    TEXT;
ALTER TABLE feed_entries ADD COLUMN IF NOT EXISTS original_summary  TEXT;
DROP INDEX  IF EXISTS idx_feed_entries_tags;
ALTER TABLE feed_entries DROP COLUMN IF EXISTS tags;

CREATE INDEX IF NOT EXISTS idx_feed_entries_feed_url
    ON feed_entries (feed_url);

CREATE INDEX IF NOT EXISTS idx_feed_entries_published_at
    ON feed_entries (published_at DESC);

CREATE INDEX IF NOT EXISTS idx_feed_entries_geo_tags
    ON feed_entries USING GIN (geo_tags);

CREATE INDEX IF NOT EXISTS idx_feed_entries_topic_tags
    ON feed_entries USING GIN (topic_tags);
"""

_INSERT_SQL = """
INSERT INTO feed_entries
    (feed_name, feed_url, guid, title, original_title, link, summary, original_summary,
     author, geo_tags, topic_tags, gist, published_at)
VALUES
    (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT ON CONSTRAINT uq_feed_entry
DO NOTHING;
"""

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def get_connection() -> PgConnection:
    """
    Open a new Postgres connection using the DATABASE_URL environment variable.
    Neon requires sslmode=require; the DSN may already include it, but we
    enforce it here as a safety net.
    """
    dsn = os.environ["DATABASE_URL"]
    # Neon requires SSL; append sslmode if not already in the DSN
    if "sslmode" not in dsn:
        dsn += ("&" if "?" in dsn else "?") + "sslmode=require"
    conn = psycopg.connect(dsn)
    conn.autocommit = False
    return conn


@contextmanager
def managed_connection() -> Generator[PgConnection, None, None]:
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------


def ensure_schema(conn: PgConnection) -> None:
    """Create tables and indexes if they don't exist yet."""
    with conn.cursor() as cur:
        cur.execute(_CREATE_TABLE_SQL)
    conn.commit()
    logger.info("Schema verified / created.")


# ---------------------------------------------------------------------------
# Insertion
# ---------------------------------------------------------------------------


def insert_entries(conn: PgConnection, entries: list[dict]) -> tuple[int, int]:
    """
    Bulk-insert feed entries, skipping duplicates.

    Returns (attempted, inserted) counts.
    """
    if not entries:
        return 0, 0

    inserted = 0
    with conn.cursor() as cur:
        for entry in entries:
            params = (
                entry["feed_name"], entry["feed_url"], entry["guid"],
                entry.get("title"), entry.get("original_title"),
                entry.get("link"),
                entry.get("summary"), entry.get("original_summary"),
                entry.get("author"),
                entry.get("geo_tags", []), entry.get("topic_tags", []),
                entry.get("gist"),
                entry["published_at"],
            )
            cur.execute(_INSERT_SQL, params)
            inserted += cur.rowcount  # 1 if inserted, 0 if skipped

    conn.commit()
    return len(entries), inserted


def fetch_untagged_entries(conn: PgConnection, feed_url: str) -> list[dict]:
    """Return entries for a given feed that still have empty topic_tags."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, feed_name, feed_url, title, summary
            FROM feed_entries
            WHERE feed_url = %s AND topic_tags = '{}'
            ORDER BY id;
            """,
            (feed_url,),
        )
        rows = cur.fetchall()
    return [
        {"id": r[0], "feed_name": r[1], "feed_url": r[2], "title": r[3], "summary": r[4]}
        for r in rows
    ]


def update_entry_tags(
    conn: PgConnection,
    entry_id: int,
    geo_tags: list[str],
    topic_tags: list[str],
) -> None:
    """Overwrite the geo and topic tags for a single entry by id."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE feed_entries SET geo_tags = %s, topic_tags = %s WHERE id = %s;",
            (geo_tags, topic_tags, entry_id),
        )
    conn.commit()
