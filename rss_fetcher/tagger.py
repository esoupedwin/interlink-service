"""
LLM-based categorization of RSS feed entries using OpenAI Structured Outputs.

Tags are split into two sets — geographic (geo_tags) and topic (topic_tags) —
each constrained at the API level via a JSON Schema enum. The model cannot
return any tag outside the lists defined in config.json.
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


def _load_config() -> dict:
    with CONFIG_FILE.open() as fh:
        return json.load(fh)


def _build_system_prompt(tagging_cfg: dict) -> str:
    geo = ", ".join(tagging_cfg["geo_categories"])
    topic = ", ".join(tagging_cfg["topic_categories"])
    return tagging_cfg["system_prompt"].format(geo_categories=geo, topic_categories=topic)


def _build_response_format(geo_categories: list[str], topic_categories: list[str]) -> dict:
    """
    Build an OpenAI Structured Outputs JSON Schema that constrains geo_tags and
    topic_tags to their respective enum lists. The model cannot emit anything else.
    """
    def _tag_array(enum: list[str], description: str) -> dict:
        return {
            "type": "array",
            "description": description,
            "items": {
                "type": "array",
                "description": "Tags for a single entry.",
                "items": {"type": "string", "enum": enum},
            },
        }

    return {
        "type": "json_schema",
        "json_schema": {
            "name": "tagging_response",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "geo_tags": _tag_array(
                        geo_categories,
                        "One inner array of geographic tags per input entry, in the same order.",
                    ),
                    "topic_tags": _tag_array(
                        topic_categories,
                        "One inner array of topic tags per input entry, in the same order.",
                    ),
                },
                "required": ["geo_tags", "topic_tags"],
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


def _normalise_length(result: list, expected: int, fallback: list) -> list:
    """Pad or truncate `result` to `expected` length, filling gaps with `fallback`."""
    if len(result) != expected:
        logger.warning(
            "Response length mismatch: got %d arrays for %d entries. Padding.",
            len(result), expected,
        )
        result = (result + [fallback for _ in range(expected)])[: expected]
    return result


def _call_openai(
    client: OpenAI,
    entries: list[dict],
    system_prompt: str,
    response_format: dict,
    default_model: str,
) -> list[dict]:
    """Returns a list of {geo_tags, topic_tags} dicts, one per entry."""
    model = os.environ.get("OPENAI_MODEL", default_model)
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
    geo_list = _normalise_length(data["geo_tags"], len(entries), [])
    topic_list = _normalise_length(data["topic_tags"], len(entries), [])

    result = []
    for i, (entry, geo, topic) in enumerate(zip(entries, geo_list, topic_list)):
        # Geo tags: empty is acceptable (some articles have no clear geo focus)
        # Topic tags: apply Misc fallback if model returned nothing
        if not topic:
            logger.warning(
                "Entry %d ('%s'): model returned empty topic_tags — applying Misc fallback.",
                i + 1, entry.get("title", "(no title)"),
            )
            topic = ["Misc"]
        result.append({"geo_tags": geo, "topic_tags": topic})

    return result


def _tag_batch_with_retry(
    client: OpenAI,
    batch: list[dict],
    batch_start: int,
    system_prompt: str,
    response_format: dict,
    default_model: str,
    retry_delay: int,
) -> list[dict]:
    """Call OpenAI for a batch, retrying once on failure."""
    for attempt in (1, 2):
        try:
            tags = _call_openai(client, batch, system_prompt, response_format, default_model)
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
                    batch_start + 1, batch_start + len(batch), exc, retry_delay,
                )
                time.sleep(retry_delay)
            else:
                logger.error(
                    "Batch %d–%d failed after 2 attempts: %s. Entries will get Misc fallback.",
                    batch_start + 1, batch_start + len(batch), exc,
                )
                return [{"geo_tags": [], "topic_tags": ["Misc"]}] * len(batch)


def tag_entries(entries: list[dict]) -> list[dict]:
    """
    Assign geographic and topic tags to each entry using OpenAI Structured Outputs.

    Returns a list of dicts with keys 'geo_tags' and 'topic_tags', one per entry.
    Tags are enforced by JSON Schema enums — only categories defined in config.json
    can appear in the output. Every entry is guaranteed at least ["Misc"] in topic_tags.
    """
    if not entries:
        return []

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        logger.warning("OPENAI_API_KEY not set — skipping tagging.")
        return [{"geo_tags": [], "topic_tags": []} for _ in entries]

    config = _load_config()
    tagging_cfg = config["tagging"]
    default_model = config.get("default_model", "gpt-4o-mini")

    system_prompt = _build_system_prompt(tagging_cfg)
    response_format = _build_response_format(
        tagging_cfg["geo_categories"], tagging_cfg["topic_categories"]
    )
    batch_size = tagging_cfg["batch_size"]
    retry_delay = tagging_cfg["retry_delay_seconds"]

    client = OpenAI(api_key=api_key)
    all_tags: list[dict] = []

    for batch_start in range(0, len(entries), batch_size):
        batch = entries[batch_start: batch_start + batch_size]
        tags = _tag_batch_with_retry(
            client, batch, batch_start,
            system_prompt, response_format,
            default_model, retry_delay,
        )
        all_tags.extend(tags)
        logger.info(
            "Tagged batch %d–%d (%d entries).",
            batch_start + 1, batch_start + len(batch), len(batch),
        )

    return all_tags
