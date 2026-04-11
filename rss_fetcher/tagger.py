"""
LLM-based categorization of RSS feed entries using OpenAI gpt-4o-mini.

Entries are sent in batches to minimize API calls and cost.
"""
import json
import logging
import os

from openai import OpenAI

logger = logging.getLogger(__name__)

CATEGORIES = [
    "United-States", "Middle-East", "AI", "Sports",
    "China", "Taiwan", "Israel", "Economy", "Space", "Misc",
]

BATCH_SIZE = 20  # entries per API call

# Override via OPENAI_MODEL env var. Defaults to gpt-4o-mini (cheapest, fast).
# Other good options: gpt-4o, gpt-4.1, gpt-4.1-mini
DEFAULT_MODEL = "gpt-4o-mini"

_SYSTEM_PROMPT = f"""You are a news categorization assistant.
Given a numbered list of news entries (title + summary), assign zero or more categories to each one.

Available categories: {", ".join(CATEGORIES)}

Rules:
- An entry may have multiple categories or none at all (empty list).
- Use "Misc" only when no other category fits and the entry is worth tagging.
- Respond ONLY with a JSON array of arrays — one inner array per entry, in the same order as the input.
- Example for 3 entries: [["United-States", "Economy"], ["AI"], []]
"""


def _build_user_prompt(entries: list[dict]) -> str:
    lines = []
    for i, entry in enumerate(entries, start=1):
        title = entry.get("title") or "(no title)"
        summary = (entry.get("summary") or "")[:300]  # truncate to keep tokens low
        lines.append(f"{i}. Title: {title}\n   Summary: {summary}")
    return "\n\n".join(lines)


def _call_openai(client: OpenAI, entries: list[dict]) -> list[list[str]]:
    model = os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)
    response = client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
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
    if isinstance(data, dict):
        result = data.get("tags", [])
    else:
        result = data

    if not isinstance(result, list) or len(result) != len(entries):
        logger.warning(
            "Unexpected tag response length (got %d, expected %d). Padding with [].",
            len(result), len(entries),
        )
        # Pad or truncate to match entry count
        result = (result + [[] for _ in entries])[: len(entries)]

    # Sanitize: keep only known categories, ensure lists
    valid = set(CATEGORIES)
    return [
        [tag for tag in (row if isinstance(row, list) else []) if tag in valid]
        for row in result
    ]


def tag_entries(entries: list[dict]) -> list[list[str]]:
    """
    Assign category tags to each entry using gpt-4o-mini.

    Returns a list of tag-lists in the same order as `entries`.
    On any error for a batch, that batch's entries receive empty tag lists.
    """
    if not entries:
        return []

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        logger.warning("OPENAI_API_KEY not set — skipping tagging.")
        return [[] for _ in entries]

    client = OpenAI(api_key=api_key)
    all_tags: list[list[str]] = []

    for batch_start in range(0, len(entries), BATCH_SIZE):
        batch = entries[batch_start: batch_start + BATCH_SIZE]
        try:
            tags = _call_openai(client, batch)
            all_tags.extend(tags)
            logger.info(
                "Tagged batch %d–%d (%d entries).",
                batch_start + 1, batch_start + len(batch), len(batch),
            )
        except Exception as exc:
            logger.error("Tagging failed for batch starting at %d: %s", batch_start, exc)
            all_tags.extend([[] for _ in batch])

    return all_tags
