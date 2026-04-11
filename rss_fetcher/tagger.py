"""
LLM-based categorization of RSS feed entries using OpenAI Structured Outputs.

Tags are constrained at the API level via a JSON Schema enum — the model
cannot return any tag outside the categories list defined in config.json.
"""
import json
import logging
import os
import time
from pathlib import Path

from openai import OpenAI

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-4o-mini"
RETRY_DELAY_SECONDS = 2

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"


def _load_config() -> dict:
    with CONFIG_FILE.open() as fh:
        return json.load(fh)["tagging"]


def _build_system_prompt(config: dict) -> str:
    categories = ", ".join(config["categories"])
    return config["system_prompt"].format(categories=categories)


def _build_response_format(categories: list[str]) -> dict:
    """
    Build an OpenAI Structured Outputs JSON Schema that constrains every tag
    to the exact strings in `categories`. The model cannot emit anything else.
    """
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "tagging_response",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "tags": {
                        "type": "array",
                        "description": "One inner array of tags per input entry, in the same order.",
                        "items": {
                            "type": "array",
                            "description": "Tags assigned to a single entry.",
                            "items": {
                                "type": "string",
                                "enum": categories,
                            },
                        },
                    }
                },
                "required": ["tags"],
                "additionalProperties": False,
            },
        },
    }


def _build_user_prompt(entries: list[dict]) -> str:
    lines = []
    for i, entry in enumerate(entries, start=1):
        title = entry.get("title") or "(no title)"
        summary = (entry.get("summary") or "")[:300]
        lines.append(f"{i}. Title: {title}\n   Summary: {summary}")
    return "\n\n".join(lines)


def _call_openai(
    client: OpenAI,
    entries: list[dict],
    system_prompt: str,
    response_format: dict,
) -> list[list[str]]:
    model = os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)
    logger.debug("Sending %d entries to model '%s'.", len(entries), model)

    response = client.chat.completions.create(
        model=model,
        response_format=response_format,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": _build_user_prompt(entries)},
        ],
        temperature=0,
    )

    raw = response.choices[0].message.content
    logger.debug("Raw model response:\n%s", raw)

    data = json.loads(raw)
    result = data["tags"]  # schema guarantees this key exists

    if len(result) != len(entries):
        logger.warning(
            "Response length mismatch: got %d tag arrays for %d entries. Padding.",
            len(result), len(entries),
        )
        result = (result + [[] for _ in entries])[: len(entries)]

    # Apply Misc fallback for any entry the model left with an empty array
    final = []
    for i, (entry, row) in enumerate(zip(entries, result)):
        if not row:
            title = entry.get("title", "(no title)")
            logger.warning(
                "Entry %d ('%s'): model returned empty tags — applying Misc fallback.",
                i + 1, title,
            )
            final.append(["Misc"])
        else:
            final.append(row)

    return final


def _tag_batch_with_retry(
    client: OpenAI,
    batch: list[dict],
    batch_start: int,
    system_prompt: str,
    response_format: dict,
) -> list[list[str]]:
    """Call OpenAI for a batch, retrying once on failure."""
    for attempt in (1, 2):
        try:
            tags = _call_openai(client, batch, system_prompt, response_format)
            if attempt == 2:
                logger.info(
                    "Batch %d–%d succeeded on retry.",
                    batch_start + 1, batch_start + len(batch),
                )
            return tags
        except Exception as exc:
            if attempt == 1:
                logger.warning(
                    "Batch %d–%d failed (attempt 1): %s. Retrying in %ds...",
                    batch_start + 1, batch_start + len(batch),
                    exc, RETRY_DELAY_SECONDS,
                )
                time.sleep(RETRY_DELAY_SECONDS)
            else:
                logger.error(
                    "Batch %d–%d failed after 2 attempts: %s. Entries will get Misc fallback.",
                    batch_start + 1, batch_start + len(batch), exc,
                )
                return [["Misc"]] * len(batch)


def tag_entries(entries: list[dict]) -> list[list[str]]:
    """
    Assign category tags to each entry using OpenAI Structured Outputs.

    Tags are enforced by a JSON Schema enum — only categories defined in
    config.json can appear in the output. Every entry is guaranteed at least
    ["Misc"] — never an empty list.
    """
    if not entries:
        return []

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        logger.warning("OPENAI_API_KEY not set — skipping tagging.")
        return [[] for _ in entries]

    config = _load_config()
    system_prompt = _build_system_prompt(config)
    response_format = _build_response_format(config["categories"])
    batch_size = config["batch_size"]

    client = OpenAI(api_key=api_key)
    all_tags: list[list[str]] = []

    for batch_start in range(0, len(entries), batch_size):
        batch = entries[batch_start: batch_start + batch_size]
        tags = _tag_batch_with_retry(client, batch, batch_start, system_prompt, response_format)
        all_tags.extend(tags)
        logger.info(
            "Tagged batch %d–%d (%d entries).",
            batch_start + 1, batch_start + len(batch), len(batch),
        )

    return all_tags
