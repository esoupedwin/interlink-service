"""
RSS Feed Fetcher — entry point.

Run this script once per schedule tick (e.g. via Railway / Render cron,
systemd timer, or any external scheduler).  It exits with:
  0  — success (even if some feeds failed; partial success is logged)
  1  — fatal error (DB unreachable, config missing, etc.)
"""
import json
import logging
import logging.handlers
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from db import ensure_schema, fetch_untagged_entries, insert_entries, managed_connection, update_entry_tags
from fetcher import fetch_feed
from summariser import summarise_entries
from tagger import tag_entries
from translator import translate_entries

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_DIR = Path(__file__).parent / "logs"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
LOG_DATE_FMT = "%Y-%m-%dT%H:%M:%S"


def _setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # stdout — always on, so GitHub Actions / terminal captures it
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FMT))

    # rotating file — daily rotation, keep 7 days
    file_handler = logging.handlers.TimedRotatingFileHandler(
        filename=LOG_DIR / "rss_fetcher.log",
        when="midnight",
        backupCount=7,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FMT))
    file_handler.suffix = "%Y-%m-%d"

    root.addHandler(stdout_handler)
    root.addHandler(file_handler)


_setup_logging()
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

        translate_entries(entries)

        tag_results = tag_entries(entries)
        for entry, tag_result in zip(entries, tag_results):
            entry["geo_tags"] = tag_result["geo_tags"]
            entry["topic_tags"] = tag_result["topic_tags"]

        gists = summarise_entries(entries)
        for entry, gist in zip(entries, gists):
            entry["gist"] = gist

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
            continue

        # Auto-backfill: re-tag any entries that landed with empty tags
        # (e.g. due to a mid-batch API error on this or a previous run)
        try:
            with managed_connection() as conn:
                untagged = fetch_untagged_entries(conn, url)
            if untagged:
                logger.warning(
                    "Feed '%s': %d entries with empty tags detected — re-tagging.",
                    name, len(untagged),
                )
                retry_tags = tag_entries(untagged)
                with managed_connection() as conn:
                    for entry, tag_result in zip(untagged, retry_tags):
                        update_entry_tags(conn, entry["id"], tag_result["geo_tags"], tag_result["topic_tags"])
                logger.info(
                    "Feed '%s': backfill complete — %d entries re-tagged.",
                    name, len(untagged),
                )
        except Exception as exc:
            logger.error("Auto-backfill failed for feed '%s': %s", name, exc)

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
