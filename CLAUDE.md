# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project purpose

INTERLINK is a scheduled news service. It fetches RSS feeds, translates non-English content, tags each article via OpenAI, scrapes the full article body to generate a gist, and stores results in Neon Postgres — producing a continuously updated, machine-tagged news database.

## Running the service

```bash
cd rss_fetcher
python main.py          # fetch all feeds, translate, tag, summarise, and insert to DB
python backfill_tags.py # re-tag all existing DB entries that have empty tags
```

## Required environment variables

Set in `rss_fetcher/.env` locally; on GitHub Actions set as repository secrets/variables.

| Variable | Required | Notes |
|---|---|---|
| `DATABASE_URL` | Yes | Neon Postgres connection string (`postgresql://...?sslmode=require`) |
| `OPENAI_API_KEY` | Yes | Translation, tagging, and summarisation are skipped if unset |
| `OPENAI_MODEL` | No | Defaults to `gpt-4o-mini` |

## Installing dependencies

```bash
pip install -r rss_fetcher/requirements.txt
```

## Architecture

The service is a single-run Python script (`main.py`) designed to be invoked by an external scheduler (GitHub Actions cron, Railway, Render, etc.) and exit. There is no long-running process.

### Data flow

```
feeds.json → fetcher.py → translator.py → tagger.py → summariser.py → db.py → Neon Postgres
```

1. **`main.py`** — orchestrator: loads feeds, calls fetcher → translator → tagger → summariser → db in sequence per feed. After inserting, queries for any entries with empty tags and auto-backfills them via `fetch_untagged_entries` + `update_entry_tags`.
2. **`fetcher.py`** — parses RSS/Atom via `feedparser`, strips HTML tags from title/summary using BeautifulSoup, normalises entries into flat dicts. `guid` falls back to `link` then `title` if `id` is absent.
3. **`translator.py`** — detects non-English entries using a non-Latin character ratio threshold (>15%). For each non-English entry: calls OpenAI individually (not batched) to translate title and summary to English, storing originals in `original_title`/`original_summary`. Individual calls guarantee 1-to-1 alignment.
4. **`tagger.py`** — sends entries to OpenAI in batches using **Structured Outputs** with a `json_schema` enum built at runtime from `config.json`. Returns separate `geo_tags` and `topic_tags` per entry. Invalid tags are impossible at the API level. Retries each batch once; applies `["Others"]`/`["Misc"]` as code-level fallbacks.
5. **`summariser.py`** — for each non-Misc entry with a link: scrapes the full article body via `httpx` + `BeautifulSoup`, then calls OpenAI individually with a JSON schema returning `{is_article: bool, gist: string}`. Gist is always in English. Returns `None` (null gist) if `is_article` is false or if scraping fails.
6. **`db.py`** — psycopg3 connection to Neon Postgres. `ensure_schema()` is idempotent (runs on every startup). Duplicates are silently skipped via `ON CONFLICT DO NOTHING` keyed on `(feed_url, guid)`.

### Configuration files

- **`rss_fetcher/feeds.json`** — list of `{name, url}` feed objects. Add/remove feeds here.
- **`rss_fetcher/config.json`** — all pipeline configuration: tag category lists (drive JSON Schema enums), system prompts for tagger/summariser/translator, batch sizes, retry delays. Editing this file alone changes pipeline behaviour without touching code.

### Key design decisions

- **Structured Outputs for tagging**: Tags are constrained by a JSON Schema `enum` built from `config.json` categories. The model cannot return an out-of-vocabulary tag. Adding or removing categories only requires editing `config.json`.
- **Split geo/topic tags**: `geo_tags TEXT[]` and `topic_tags TEXT[]` are stored as separate columns, each with their own GIN index. `Transnational` is the geo fallback for global tech companies (NVIDIA, Meta, etc.); `Others` for anything else unmatched. `Misc` is the topic fallback.
- **Individual translation and summarisation calls**: Both translator.py and summariser.py call OpenAI once per entry (not batched) to guarantee 1-to-1 alignment. Batching caused count mismatches that left entries untranslated/ungisted.
- **`is_article` detection**: The summariser's JSON schema includes a boolean `is_article` field. The model sets this to `false` for video pages, promotional content, and navigation pages — these get `gist = NULL` without a separate classification step.
- **Auto-backfill**: After each feed's insert, `main.py` queries for entries with empty `topic_tags` and re-tags them. This recovers from mid-batch API failures on the current or previous runs.

### Database schema

Single table `feed_entries`: `id`, `feed_name`, `feed_url`, `guid`, `title`, `original_title`, `link`, `summary`, `original_summary`, `author`, `geo_tags TEXT[]`, `topic_tags TEXT[]`, `gist TEXT`, `published_at`, `fetched_at`. Unique constraint on `(feed_url, guid)`. GIN indexes on both tag columns.

### Logging

Logs go to both stdout and `rss_fetcher/logs/rss_fetcher.log` (daily rotation, 7-day retention). The `logs/` directory is gitignored.

### Scheduling

GitHub Actions workflow at `.github/workflows/rss_fetcher.yml` runs on `0 */4 * * *` (every 4 hours). `OPENAI_MODEL` is set via a repository **variable** (`vars.OPENAI_MODEL`); `DATABASE_URL` and `OPENAI_API_KEY` are repository **secrets**.
