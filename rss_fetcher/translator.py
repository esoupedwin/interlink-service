"""
Detects non-English RSS entries and translates their title and summary to English.

For each entry:
- Skipped if title and summary are already English (or empty)
- original_title and original_summary are set to the raw values from the feed
- title and summary are replaced with English translations
- On translation failure, originals are kept in title/summary and still stored
  in original_title/original_summary so the record is never lost
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
# (CJK, Arabic, Korean, Cyrillic with heavy use, Thai, Hebrew, etc.)
_NON_LATIN_THRESHOLD = 0.15  # >15% non-Latin chars → treat as non-English

_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "translation_response",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "translations": {
                    "type": "array",
                    "description": "One object per input entry, in the same order.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title":   {"type": "string", "description": "English translation of the title."},
                            "summary": {"type": "string", "description": "English translation of the summary. Empty string if no summary was provided."},
                        },
                        "required": ["title", "summary"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["translations"],
            "additionalProperties": False,
        },
    },
}

_SYSTEM_PROMPT = (
    "You are a professional translator. Translate each provided news title and summary into "
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


def _build_user_prompt(entries: list[dict]) -> str:
    lines = []
    for i, entry in enumerate(entries, start=1):
        title = entry.get("title") or ""
        summary = entry.get("summary") or ""
        lines.append(f"{i}.\nTitle: {title}\nSummary: {summary}")
    return "\n\n".join(lines)


def _call_openai_batch(client: OpenAI, entries: list[dict], default_model: str) -> list[dict]:
    """
    Translate a batch of entries. Returns a list of {title, summary} dicts.
    Raises on API error so the caller can retry.
    """
    model = os.environ.get("OPENAI_MODEL", default_model)
    response = client.chat.completions.create(
        model=model,
        response_format=_RESPONSE_FORMAT,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(entries)},
        ],
        temperature=0,
    )
    data = json.loads(response.choices[0].message.content)
    result = data["translations"]

    if len(result) != len(entries):
        logger.warning(
            "Translation response length mismatch: got %d for %d entries. Padding with originals.",
            len(result), len(entries),
        )
        fallbacks = [{"title": e.get("title") or "", "summary": e.get("summary") or ""}
                     for e in entries]
        result = (result + fallbacks)[: len(entries)]

    return result


def _translate_batch_with_retry(
    client: OpenAI,
    batch: list[dict],
    batch_indices: list[int],
    default_model: str,
    retry_delay: int,
) -> list[dict]:
    """Translate a batch, retrying once on failure. Returns originals on total failure."""
    for attempt in (1, 2):
        try:
            translated = _call_openai_batch(client, batch, default_model)
            if attempt == 2:
                logger.info("Translation batch succeeded on retry.")
            return translated
        except Exception as exc:
            if attempt == 1:
                logger.warning(
                    "Translation batch failed (attempt 1): %s. Retrying in %ds...",
                    exc, retry_delay,
                )
                time.sleep(retry_delay)
            else:
                logger.error(
                    "Translation batch failed after 2 attempts: %s. Keeping originals.", exc,
                )
                return [
                    {"title": e.get("title") or "", "summary": e.get("summary") or ""}
                    for e in batch
                ]


def translate_entries(entries: list[dict]) -> list[dict]:
    """
    Detect non-English entries and translate title/summary to English in place.

    For translated entries:
    - original_title / original_summary hold the raw feed values
    - title / summary are replaced with English translations

    For English entries original_title and original_summary are not set (callers
    should treat their absence as None when writing to the database).

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
    batch_size = translator_cfg.get("batch_size", 20)
    retry_delay = translator_cfg.get("retry_delay_seconds", 2)

    # Identify which entries need translation
    to_translate = [(i, entry) for i, entry in enumerate(entries) if _needs_translation(entry)]

    if not to_translate:
        logger.info("All entries are English — no translation needed.")
        return entries

    logger.info("%d/%d entries require translation.", len(to_translate), len(entries))

    client = OpenAI(api_key=api_key)
    n_translated = 0
    n_failed = 0

    for batch_start in range(0, len(to_translate), batch_size):
        batch_slice = to_translate[batch_start: batch_start + batch_size]
        batch_indices = [i for i, _ in batch_slice]
        batch_entries = [e for _, e in batch_slice]

        results = _translate_batch_with_retry(
            client, batch_entries, batch_indices, default_model, retry_delay,
        )

        for (orig_idx, entry), result in zip(batch_slice, results):
            title_en = result["title"].strip() or entry.get("title")
            summary_en = result["summary"].strip() or entry.get("summary")

            # Detect if translation actually changed anything
            if title_en != entry.get("title") or summary_en != entry.get("summary"):
                entry["original_title"] = entry.get("title")
                entry["original_summary"] = entry.get("summary")
                entry["title"] = title_en
                entry["summary"] = summary_en
                logger.info(
                    "Entry %d: translated — '%s'",
                    orig_idx + 1, entry.get("title", ""),
                )
                n_translated += 1
            else:
                logger.debug("Entry %d: translation returned identical text.", orig_idx + 1)
                n_failed += 1

    logger.info(
        "Translation complete — translated: %d | unchanged/failed: %d.",
        n_translated, n_failed,
    )
    return entries
