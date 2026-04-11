"""
One-shot backfill: find all feed_entries with empty tags and tag them.

Usage:
    python backfill_tags.py
"""
import logging
import os
import sys

from dotenv import load_dotenv

from db import managed_connection
from tagger import tag_entries

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("backfill_tags")

_FETCH_SQL = """
SELECT id, feed_name, feed_url, title, summary
FROM feed_entries
WHERE tags = '{}'
ORDER BY id;
"""

_UPDATE_SQL = "UPDATE feed_entries SET tags = %s WHERE id = %s;"


def run() -> None:
    load_dotenv()

    if not os.environ.get("DATABASE_URL"):
        logger.error("DATABASE_URL not set.")
        sys.exit(1)

    if not os.environ.get("OPENAI_API_KEY"):
        logger.error("OPENAI_API_KEY not set.")
        sys.exit(1)

    with managed_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_FETCH_SQL)
            rows = cur.fetchall()

    if not rows:
        logger.info("No untagged entries found.")
        return

    logger.info("Found %d untagged entries. Tagging now...", len(rows))

    # Build entry dicts the tagger expects
    entries = [
        {"id": r[0], "feed_name": r[1], "feed_url": r[2], "title": r[3], "summary": r[4]}
        for r in rows
    ]

    tags = tag_entries(entries)

    updated = 0
    with managed_connection() as conn:
        with conn.cursor() as cur:
            for entry, entry_tags in zip(entries, tags):
                cur.execute(_UPDATE_SQL, (entry_tags, entry["id"]))
                updated += cur.rowcount

    logger.info("Backfill complete. %d entries updated.", updated)


if __name__ == "__main__":
    run()
