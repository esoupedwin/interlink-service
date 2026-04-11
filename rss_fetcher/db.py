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
    title        TEXT,
    link         TEXT,
    summary      TEXT,
    author       TEXT,
    published_at TIMESTAMPTZ,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_feed_entry UNIQUE (feed_url, guid)
);

CREATE INDEX IF NOT EXISTS idx_feed_entries_feed_url
    ON feed_entries (feed_url);

CREATE INDEX IF NOT EXISTS idx_feed_entries_published_at
    ON feed_entries (published_at DESC);
"""

_INSERT_SQL = """
INSERT INTO feed_entries
    (feed_name, feed_url, guid, title, link, summary, author, published_at)
VALUES
    (%s, %s, %s, %s, %s, %s, %s, %s)
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
                entry["title"], entry["link"], entry["summary"],
                entry["author"], entry["published_at"],
            )
            cur.execute(_INSERT_SQL, params)
            inserted += cur.rowcount  # 1 if inserted, 0 if skipped

    conn.commit()
    return len(entries), inserted
