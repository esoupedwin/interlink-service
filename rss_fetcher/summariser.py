"""
Article scraping and LLM gist generation.

For each entry:
- Skipped entirely if tags contain "Misc" (returns None)
- Full article body is scraped from entry["link"]
- OpenAI generates a 2-3 sentence gist
- Scrape or API failures return None gracefully
"""
import logging
import os
import time
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from openai import OpenAI

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-4o-mini"
RETRY_DELAY_SECONDS = 2
SCRAPE_TIMEOUT = 10     # seconds
MAX_ARTICLE_CHARS = 4000  # truncate scraped body to control token usage

_SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; INTERLINKBot/1.0; +https://github.com/esoupedwin/interlink-service)"
    )
}

_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "summarisation_response",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "is_article": {
                    "type": "boolean",
                    "description": "True if the content is a news article. False if it is promotional, an advertisement, a navigation page, or otherwise not editorial news content.",
                },
                "gist": {
                    "type": "string",
                    "description": "A concise 2-3 sentence summary of the article. Empty string if is_article is false.",
                },
            },
            "required": ["is_article", "gist"],
            "additionalProperties": False,
        },
    },
}

_SYSTEM_PROMPT = (
    "You are a news summariser. First determine whether the provided content is a genuine "
    "news article. If it is promotional content, an advertisement, a navigation/index page, "
    "or otherwise not editorial news content, set is_article to false and gist to an empty string. "
    "If it is a news article, set is_article to true and write a concise 2-3 sentence gist "
    "capturing the key facts and significance."
)


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

def _scrape_article(url: str) -> str | None:
    """
    Fetch and extract the main text body of an article URL.
    Returns None on any network or parse error.
    """
    try:
        response = httpx.get(
            url,
            headers=_SCRAPE_HEADERS,
            timeout=SCRAPE_TIMEOUT,
            follow_redirects=True,
        )
        response.raise_for_status()
    except Exception as exc:
        logger.warning("Failed to scrape '%s': %s", url, exc)
        return None

    soup = BeautifulSoup(response.text, "lxml")

    # Remove noise
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "figure"]):
        tag.decompose()

    # Try semantic content containers in priority order
    body = (
        soup.find("article")
        or soup.find("main")
        or soup.find(class_=lambda c: c and any(
            k in c.lower() for k in ("article-body", "article__body", "story-body", "post-content", "entry-content")
        ))
    )

    if body:
        text = body.get_text(separator=" ", strip=True)
    else:
        # Fallback: join all <p> tags
        text = " ".join(p.get_text(strip=True) for p in soup.find_all("p"))

    text = " ".join(text.split())  # normalise whitespace
    if not text:
        logger.warning("No article text extracted from '%s'.", url)
        return None

    return text[:MAX_ARTICLE_CHARS]


# ---------------------------------------------------------------------------
# Summarisation
# ---------------------------------------------------------------------------

def _call_openai_single(client: OpenAI, article_text: str) -> str | None:
    import json
    model = os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)
    response = client.chat.completions.create(
        model=model,
        response_format=_RESPONSE_FORMAT,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": article_text},
        ],
        temperature=0,
    )
    data = json.loads(response.choices[0].message.content)
    if not data["is_article"]:
        return None
    return data["gist"] or None


def _summarise_with_retry(client: OpenAI, orig_idx: int, article_text: str) -> str | None:
    for attempt in (1, 2):
        try:
            gist = _call_openai_single(client, article_text)
            if attempt == 2:
                logger.info("Entry %d summarisation succeeded on retry.", orig_idx + 1)
            return gist
        except Exception as exc:
            if attempt == 1:
                logger.warning(
                    "Entry %d summarisation failed (attempt 1): %s. Retrying in %ds...",
                    orig_idx + 1, exc, RETRY_DELAY_SECONDS,
                )
                time.sleep(RETRY_DELAY_SECONDS)
            else:
                logger.error(
                    "Entry %d summarisation failed after 2 attempts: %s.",
                    orig_idx + 1, exc,
                )
                return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def summarise_entries(entries: list[dict]) -> list[str | None]:
    """
    Generate a gist for each entry.

    - Returns None for entries tagged with "Misc" (skipped by design).
    - Returns None for entries where scraping or summarisation fails.
    - Returns a gist string for all other entries.

    Result list is the same length and order as `entries`.
    """
    if not entries:
        return []

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        logger.warning("OPENAI_API_KEY not set — skipping summarisation.")
        return [None] * len(entries)

    client = OpenAI(api_key=api_key)
    gists: list[str | None] = [None] * len(entries)

    # Determine which entries qualify (not Misc-tagged, has a link)
    to_scrape: list[tuple[int, dict]] = []  # (original_index, entry)
    for i, entry in enumerate(entries):
        tags = entry.get("tags") or []
        if "Misc" in tags:
            logger.debug("Entry %d ('%s'): skipping summarisation (tagged Misc).", i + 1, entry.get("title"))
            continue
        if not entry.get("link"):
            logger.debug("Entry %d: skipping summarisation (no link).", i + 1)
            continue
        to_scrape.append((i, entry))

    if not to_scrape:
        logger.info("No entries eligible for summarisation.")
        return gists

    logger.info("%d entries eligible for summarisation. Scraping articles...", len(to_scrape))

    # Scrape articles
    scraped: list[tuple[int, str]] = []  # (original_index, article_text)
    for i, entry in to_scrape:
        text = _scrape_article(entry["link"])
        if text:
            scraped.append((i, text))
        else:
            logger.warning(
                "Entry %d ('%s'): scrape failed — gist will be None.",
                i + 1, entry.get("title"),
            )

    if not scraped:
        logger.warning("All scrapes failed — no gists generated.")
        return gists

    logger.info("%d/%d articles scraped successfully. Generating gists...", len(scraped), len(to_scrape))

    # Summarise one article at a time to guarantee 1-to-1 alignment
    for orig_idx, article_text in scraped:
        gist_text = _summarise_with_retry(client, orig_idx, article_text)
        gists[orig_idx] = gist_text
        logger.debug("Entry %d gist generated.", orig_idx + 1)

    logger.info("Summarisation complete. %d gists generated.", sum(1 for g in gists if g))
    return gists
