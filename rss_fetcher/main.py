"""
RSS Feed Fetcher — entry point.

Run this script once per schedule tick (e.g. via Railway / Render cron,
systemd timer, or any external scheduler).  It exits with:
  0  — success (even if some feeds failed; partial success is logged)
  1  — fatal error (DB unreachable, config missing, etc.)
"""
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from db import ensure_schema, insert_entries, managed_connection
from fetcher import fetch_feed
from tagger import tag_entries

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("rss_fetcher")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
FEEDS_FILE = BASE_DIR / "feeds.json"


def load_feeds() -> list[dict]:
    if not FEEDS_FILE.exists():
        raise FileNotFoundError(f"feeds.json not found at {FEEDS_FILE}")
    with FEEDS_FILE.open() as fh:
        feeds = json.load(fh)
    if not isinstance(feeds, list) or not feeds:
        raise ValueError("feeds.json must be a non-empty JSON array")
    for feed in feeds:
        if not feed.get("name") or not feed.get("url"):
            raise ValueError(f"Each feed must have 'name' and 'url'. Got: {feed}")
    return feeds


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run() -> None:
    load_dotenv()  # loads .env if present; no-op on platforms with real env vars

    if not os.environ.get("DATABASE_URL"):
        logger.error("DATABASE_URL environment variable is not set.")
        sys.exit(1)

    try:
        feeds = load_feeds()
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        logger.error("Failed to load feeds config: %s", exc)
        sys.exit(1)

    logger.info("Starting RSS fetch run. Feeds to process: %d", len(feeds))

    try:
        with managed_connection() as conn:
            ensure_schema(conn)
    except Exception as exc:
        logger.error("Cannot connect to database or create schema: %s", exc)
        sys.exit(1)

    total_attempted = 0
    total_inserted = 0
    failed_feeds = []

    for feed in feeds:
        name, url = feed["name"], feed["url"]
        try:
            entries = fetch_feed(name, url)
        except Exception as exc:
            logger.error("Error fetching feed '%s': %s", name, exc)
            failed_feeds.append(name)
            continue

        if not entries:
            logger.info("Feed '%s': no entries to insert.", name)
            continue

        tags = tag_entries(entries)
        for entry, entry_tags in zip(entries, tags):
            entry["tags"] = entry_tags

        try:
            with managed_connection() as conn:
                attempted, inserted = insert_entries(conn, entries)
            total_attempted += attempted
            total_inserted += inserted
            skipped = attempted - inserted
            logger.info(
                "Feed '%s': %d inserted, %d skipped (duplicates).",
                name, inserted, skipped,
            )
        except Exception as exc:
            logger.error("DB error inserting entries for feed '%s': %s", name, exc)
            failed_feeds.append(name)

    logger.info(
        "Run complete. Entries: %d attempted, %d inserted. Failed feeds: %s",
        total_attempted,
        total_inserted,
        failed_feeds or "none",
    )

    if failed_feeds:
        # Exit non-zero so the scheduler / monitoring can flag partial failure
        sys.exit(2)


if __name__ == "__main__":
    run()
