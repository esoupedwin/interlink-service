"""
Article scraping and LLM gist generation.

For each entry:
- Skipped entirely if topic_tags contain "Misc" (returns None)
- Full article body is scraped from entry["link"]
- OpenAI generates a 2-3 sentence gist
- Scrape or API failures return None gracefully
"""
import json
import logging
import os
import time
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from openai import OpenAI

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"

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
                    "description": "A concise 2-3 sentence summary of the article written in English. Must always be in English regardless of the language of the source article. Empty string if is_article is false.",
                },
            },
            "required": ["is_article", "gist"],
            "additionalProperties": False,
        },
    },
}

# Sentinel so callers can distinguish "not a news article" from an API error
_NOT_ARTICLE = object()


def _load_config() -> dict:
    with CONFIG_FILE.open() as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

def _scrape_article(url: str, timeout: int, max_chars: int) -> str | None:
    """
    Fetch and extract the main text body of an article URL.
    Returns None on any network or parse error.
    """
    try:
        response = httpx.get(
            url,
            headers=_SCRAPE_HEADERS,
            timeout=timeout,
            follow_redirects=True,
        )
        response.raise_for_status()
    except Exception as exc:
        logger.warning("Scrape failed: %s", exc)
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

    return text[:max_chars]


# ---------------------------------------------------------------------------
# Summarisation
# ---------------------------------------------------------------------------

def _call_openai_single(
    client: OpenAI,
    article_text: str,
    system_prompt: str,
    default_model: str,
) -> str | object:
    """
    Returns a gist string, or the _NOT_ARTICLE sentinel if the model determined
    the content is not a news article.
    Raises on API errors so the caller can retry.
    """
    model = os.environ.get("OPENAI_MODEL", default_model)
    response = client.chat.completions.create(
        model=model,
        response_format=_RESPONSE_FORMAT,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": article_text},
        ],
        temperature=0,
    )
    data = json.loads(response.choices[0].message.content)
    if not data["is_article"]:
        return _NOT_ARTICLE
    return data["gist"] or None


def _summarise_with_retry(
    client: OpenAI,
    orig_idx: int,
    entry_title: str,
    article_text: str,
    system_prompt: str,
    default_model: str,
    retry_delay: int,
) -> str | object | None:
    """
    Returns a gist string, _NOT_ARTICLE sentinel, or None on total failure.
    """
    for attempt in (1, 2):
        try:
            result = _call_openai_single(client, article_text, system_prompt, default_model)
            if attempt == 2:
                logger.info(
                    "Entry %d ('%s'): summarisation succeeded on retry.",
                    orig_idx + 1, entry_title,
                )
            return result
        except Exception as exc:
            if attempt == 1:
                logger.warning(
                    "Entry %d ('%s'): summarisation failed (attempt 1): %s. Retrying in %ds...",
                    orig_idx + 1, entry_title, exc, retry_delay,
                )
                time.sleep(retry_delay)
            else:
                logger.error(
                    "Entry %d ('%s'): summarisation failed after 2 attempts: %s.",
                    orig_idx + 1, entry_title, exc,
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

    config = _load_config()
    summariser_cfg = config["summariser"]
    default_model = config.get("default_model", "gpt-4o-mini")

    system_prompt = summariser_cfg["system_prompt"]
    scrape_timeout = summariser_cfg["scrape_timeout"]
    max_article_chars = summariser_cfg["max_article_chars"]
    retry_delay = summariser_cfg["retry_delay_seconds"]

    client = OpenAI(api_key=api_key)
    gists: list[str | None] = [None] * len(entries)

    # Outcome counters for end-of-run breakdown
    n_misc = 0
    n_no_link = 0
    n_scrape_fail = 0
    n_not_article = 0
    n_api_fail = 0
    n_gisted = 0

    # Determine which entries qualify (not Misc-tagged, has a link)
    to_scrape: list[tuple[int, dict]] = []
    for i, entry in enumerate(entries):
        title = entry.get("title") or "(no title)"
        topic_tags = entry.get("topic_tags") or []
        if "Misc" in topic_tags:
            logger.info(
                "Entry %d ('%s'): skipped — tagged Misc.",
                i + 1, title,
            )
            n_misc += 1
            continue
        if not entry.get("link"):
            logger.info(
                "Entry %d ('%s'): skipped — no link.",
                i + 1, title,
            )
            n_no_link += 1
            continue
        to_scrape.append((i, entry))

    if not to_scrape:
        logger.info("No entries eligible for summarisation.")
        return gists

    logger.info("%d entries eligible for summarisation. Scraping articles...", len(to_scrape))

    # Scrape articles
    scraped: list[tuple[int, dict, str]] = []  # (original_index, entry, article_text)
    for i, entry in to_scrape:
        title = entry.get("title") or "(no title)"
        text = _scrape_article(entry["link"], scrape_timeout, max_article_chars)
        if text:
            scraped.append((i, entry, text))
        else:
            logger.warning(
                "Entry %d ('%s'): gist=None — scrape failed (%s).",
                i + 1, title, entry["link"],
            )
            n_scrape_fail += 1

    if not scraped:
        logger.warning("All scrapes failed — no gists generated.")
        return gists

    logger.info("%d/%d articles scraped. Generating gists...", len(scraped), len(to_scrape))

    # Summarise one article at a time to guarantee 1-to-1 alignment
    for orig_idx, entry, article_text in scraped:
        title = entry.get("title") or "(no title)"
        result = _summarise_with_retry(
            client, orig_idx, title, article_text,
            system_prompt, default_model, retry_delay,
        )
        if result is _NOT_ARTICLE:
            logger.info(
                "Entry %d ('%s'): gist=None — not a news article (promotional/video/nav page).",
                orig_idx + 1, title,
            )
            n_not_article += 1
        elif result is None:
            logger.warning(
                "Entry %d ('%s'): gist=None — API failure after retries.",
                orig_idx + 1, title,
            )
            n_api_fail += 1
        else:
            gists[orig_idx] = result
            n_gisted += 1

    logger.info(
        "Summarisation complete — gisted: %d | not_article: %d | scrape_fail: %d"
        " | api_fail: %d | misc: %d | no_link: %d",
        n_gisted, n_not_article, n_scrape_fail, n_api_fail, n_misc, n_no_link,
    )
    return gists
