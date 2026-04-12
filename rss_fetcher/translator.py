"""
Detects non-English RSS entries and translates their title and summary to English.

For each entry:
- Skipped if title and summary are already English (or empty)
- original_title and original_summary are set to the raw values from the feed
- title and summary are replaced with English translations
- On translation failure, originals are kept in title/summary and still stored
  in original_title/original_summary so the record is never lost

Individual per-entry API calls are used (not batching) to guarantee 1-to-1
alignment — batching caused count mismatches that left some entries untranslated.
"""
import json
import logging
import os
import time
from pathlib import Path

from openai import OpenAI

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"

# Characters above Latin Extended-B (U+024F) indicate non-Latin scripts
# (CJK, Arabic, Korean, Thai, Hebrew, etc.)
_NON_LATIN_THRESHOLD = 0.15  # >15% non-Latin chars → treat as non-English

_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "translation_response",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "English translation of the title.",
                },
                "summary": {
                    "type": "string",
                    "description": "English translation of the summary. Empty string if no summary was provided.",
                },
            },
            "required": ["title", "summary"],
            "additionalProperties": False,
        },
    },
}

_SYSTEM_PROMPT = (
    "You are a professional translator. Translate the provided news title and summary into "
    "natural, fluent English. Preserve the factual meaning exactly. Do not summarise, add, "
    "or omit information. If a field is already in English, return it unchanged."
)


def _load_config() -> dict:
    with CONFIG_FILE.open() as fh:
        return json.load(fh)


def _is_non_english(text: str) -> bool:
    """Return True if >15% of characters are outside the Latin script range."""
    if not text:
        return False
    non_latin = sum(1 for c in text if ord(c) > 0x024F)
    return (non_latin / len(text)) > _NON_LATIN_THRESHOLD


def _needs_translation(entry: dict) -> bool:
    return _is_non_english(entry.get("title") or "") or _is_non_english(entry.get("summary") or "")


def _call_openai_single(client: OpenAI, entry: dict, default_model: str) -> dict:
    """
    Translate a single entry's title and summary.
    Returns {title, summary} with English text.
    Raises on API error so the caller can retry.
    """
    model = os.environ.get("OPENAI_MODEL", default_model)
    user_content = f"Title: {entry.get('title') or ''}\nSummary: {entry.get('summary') or ''}"
    response = client.chat.completions.create(
        model=model,
        response_format=_RESPONSE_FORMAT,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        temperature=0,
    )
    return json.loads(response.choices[0].message.content)


def _translate_with_retry(
    client: OpenAI,
    orig_idx: int,
    entry: dict,
    default_model: str,
    retry_delay: int,
) -> dict | None:
    """
    Translate a single entry with one retry on failure.
    Returns {title, summary} on success, None on total failure.
    """
    for attempt in (1, 2):
        try:
            result = _call_openai_single(client, entry, default_model)
            if attempt == 2:
                logger.info("Entry %d: translation succeeded on retry.", orig_idx + 1)
            return result
        except Exception as exc:
            if attempt == 1:
                logger.warning(
                    "Entry %d ('%s'): translation failed (attempt 1): %s. Retrying in %ds...",
                    orig_idx + 1, entry.get("title", ""), exc, retry_delay,
                )
                time.sleep(retry_delay)
            else:
                logger.error(
                    "Entry %d ('%s'): translation failed after 2 attempts: %s. Keeping original.",
                    orig_idx + 1, entry.get("title", ""), exc,
                )
                return None


def translate_entries(entries: list[dict]) -> list[dict]:
    """
    Detect non-English entries and translate title/summary to English in place.

    For translated entries:
    - original_title / original_summary hold the raw feed values
    - title / summary are replaced with English translations

    For English entries, original_title and original_summary are not set
    (stored as NULL in the database).

    Returns the same list (mutated in place) for convenience.
    """
    if not entries:
        return entries

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        logger.warning("OPENAI_API_KEY not set — skipping translation.")
        return entries

    config = _load_config()
    translator_cfg = config.get("translator", {})
    default_model = config.get("default_model", "gpt-4o-mini")
    retry_delay = translator_cfg.get("retry_delay_seconds", 2)

    to_translate = [(i, entry) for i, entry in enumerate(entries) if _needs_translation(entry)]

    if not to_translate:
        logger.info("All entries are English — no translation needed.")
        return entries

    logger.info("%d/%d entries require translation.", len(to_translate), len(entries))

    client = OpenAI(api_key=api_key)
    n_translated = 0
    n_failed = 0

    for orig_idx, entry in to_translate:
        result = _translate_with_retry(client, orig_idx, entry, default_model, retry_delay)

        if result is None:
            # API failure — store original in both columns so nothing is lost
            entry["original_title"] = entry.get("title")
            entry["original_summary"] = entry.get("summary")
            n_failed += 1
            continue

        title_en = result["title"].strip() or entry.get("title")
        summary_en = result["summary"].strip() or entry.get("summary")

        entry["original_title"] = entry.get("title")
        entry["original_summary"] = entry.get("summary")
        entry["title"] = title_en
        entry["summary"] = summary_en

        logger.info(
            "Entry %d: translated — '%s'",
            orig_idx + 1, title_en,
        )
        n_translated += 1

    logger.info(
        "Translation complete — translated: %d | failed: %d.",
        n_translated, n_failed,
    )
    return entries
