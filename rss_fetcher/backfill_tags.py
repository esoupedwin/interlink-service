"""
One-shot backfill: find all feed_entries with empty topic_tags and tag them.

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
WHERE topic_tags = '{}'
ORDER BY id;
"""

_UPDATE_SQL = "UPDATE feed_entries SET geo_tags = %s, topic_tags = %s WHERE id = %s;"


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

    entries = [
        {"id": r[0], "feed_name": r[1], "feed_url": r[2], "title": r[3], "summary": r[4]}
        for r in rows
    ]

    tag_results = tag_entries(entries)

    updated = 0
    with managed_connection() as conn:
        with conn.cursor() as cur:
            for entry, tag_result in zip(entries, tag_results):
                cur.execute(_UPDATE_SQL, (tag_result["geo_tags"], tag_result["topic_tags"], entry["id"]))
                updated += cur.rowcount

    logger.info("Backfill complete. %d entries updated.", updated)


if __name__ == "__main__":
    run()
