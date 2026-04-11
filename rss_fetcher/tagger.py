"""
LLM-based categorization of RSS feed entries using OpenAI.

Entries are sent in batches to minimize API calls and cost.
Configuration (categories, prompt, batch size) is loaded from config.json.
"""
import json
import logging
import os
from pathlib import Path

from openai import OpenAI

logger = logging.getLogger(__name__)

# Override via OPENAI_MODEL env var. Defaults to gpt-4o-mini (cheapest, fast).
DEFAULT_MODEL = "gpt-4o-mini"

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"


def _load_config() -> dict:
    with CONFIG_FILE.open() as fh:
        return json.load(fh)["tagging"]


def _build_system_prompt(config: dict) -> str:
    categories = ", ".join(config["categories"])
    return config["system_prompt"].format(categories=categories)


def _build_user_prompt(entries: list[dict]) -> str:
    lines = []
    for i, entry in enumerate(entries, start=1):
        title = entry.get("title") or "(no title)"
        summary = (entry.get("summary") or "")[:300]  # truncate to keep tokens low
        lines.append(f"{i}. Title: {title}\n   Summary: {summary}")
    return "\n\n".join(lines)


def _call_openai(
    client: OpenAI,
    entries: list[dict],
    system_prompt: str,
    valid_categories: set[str],
) -> list[list[str]]:
    model = os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)
    response = client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    "Categorize the following entries and return a JSON object "
                    "with a single key \"tags\" whose value is an array of arrays.\n\n"
                    + _build_user_prompt(entries)
                ),
            },
        ],
        temperature=0,
    )

    raw = response.choices[0].message.content
    data = json.loads(raw)

    # Accept {"tags": [[...]]} or bare [[...]]
    result = data.get("tags", []) if isinstance(data, dict) else data

    if not isinstance(result, list) or len(result) != len(entries):
        logger.warning(
            "Unexpected tag response length (got %d, expected %d). Padding with [].",
            len(result), len(entries),
        )
        result = (result + [[] for _ in entries])[: len(entries)]

    # Sanitize: keep only known categories, ensure lists
    return [
        [tag for tag in (row if isinstance(row, list) else []) if tag in valid_categories]
        for row in result
    ]


def tag_entries(entries: list[dict]) -> list[list[str]]:
    """
    Assign category tags to each entry.

    Returns a list of tag-lists in the same order as `entries`.
    On any error for a batch, that batch's entries receive empty tag lists.
    """
    if not entries:
        return []

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        logger.warning("OPENAI_API_KEY not set — skipping tagging.")
        return [[] for _ in entries]

    config = _load_config()
    system_prompt = _build_system_prompt(config)
    valid_categories = set(config["categories"])
    batch_size = config["batch_size"]

    client = OpenAI(api_key=api_key)
    all_tags: list[list[str]] = []

    for batch_start in range(0, len(entries), batch_size):
        batch = entries[batch_start: batch_start + batch_size]
        try:
            tags = _call_openai(client, batch, system_prompt, valid_categories)
            all_tags.extend(tags)
            logger.info(
                "Tagged batch %d–%d (%d entries).",
                batch_start + 1, batch_start + len(batch), len(batch),
            )
        except Exception as exc:
            logger.error("Tagging failed for batch starting at %d: %s", batch_start, exc)
            all_tags.extend([[] for _ in batch])

    return all_tags
