"""
RSS / Atom feed fetching and normalization.
"""
import logging
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser

logger = logging.getLogger(__name__)


def _parse_date(entry: feedparser.FeedParserDict) -> datetime | None:
    """
    Return a timezone-aware datetime from the best available date field,
    or None if no parseable date is found.
    """
    # feedparser exposes parsed structs in *_parsed fields (time.struct_time UTC)
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(field)
        if value:
            try:
                return datetime(*value[:6], tzinfo=timezone.utc)
            except Exception:
                pass

    # Fallback: raw string fields
    for field in ("published", "updated", "created"):
        raw = entry.get(field)
        if raw:
            try:
                return parsedate_to_datetime(raw)
            except Exception:
                pass

    return None


def _normalize_entry(feed_name: str, feed_url: str, entry: feedparser.FeedParserDict) -> dict:
    """
    Map a feedparser entry to the flat dict expected by db.insert_entries.
    """
    # guid: prefer id, fall back to link, then title
    guid = (
        entry.get("id")
        or entry.get("link")
        or entry.get("title")
        or ""
    ).strip()

    summary = entry.get("summary") or entry.get("content", [{}])[0].get("value", "")

    return {
        "feed_name": feed_name,
        "feed_url": feed_url,
        "guid": guid,
        "title": (entry.get("title") or "").strip() or None,
        "link": (entry.get("link") or "").strip() or None,
        "summary": summary.strip() or None,
        "author": (entry.get("author") or "").strip() or None,
        "published_at": _parse_date(entry),
    }


def fetch_feed(name: str, url: str) -> list[dict]:
    """
    Fetch and parse a single RSS/Atom feed.

    Returns a list of normalized entry dicts ready for database insertion.
    Raises on network or parse errors so the caller can log and continue.
    """
    logger.info("Fetching feed '%s' from %s", name, url)

    parsed = feedparser.parse(url)

    # feedparser doesn't raise — check bozo flag for malformed feeds
    if parsed.bozo:
        exc = parsed.get("bozo_exception")
        logger.warning("Feed '%s' is malformed: %s", name, exc)

    status = parsed.get("status", 0)
    if status and status >= 400:
        raise RuntimeError(f"HTTP {status} fetching feed '{name}' ({url})")

    entries = [
        _normalize_entry(name, url, entry)
        for entry in parsed.entries
        if entry  # skip empty entries
    ]

    # Drop entries with no usable guid
    valid = [e for e in entries if e["guid"]]
    skipped = len(entries) - len(valid)
    if skipped:
        logger.warning("Feed '%s': dropped %d entries with no guid/link/title", name, skipped)

    logger.info("Feed '%s': %d entries fetched.", name, len(valid))
    return valid
