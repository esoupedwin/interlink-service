# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project purpose

INTERLINK is a scheduled news service. It fetches RSS feeds, tags each article via OpenAI, and stores results in Neon Postgres — producing a continuously updated, machine-tagged news database.

## Running the service

```bash
cd rss_fetcher
python main.py          # fetch all feeds, tag, and insert to DB
python backfill_tags.py # re-tag all existing DB entries that have empty tags
```

## Required environment variables

Set in `rss_fetcher/.env` locally; on GitHub Actions set as repository secrets/variables.

| Variable | Required | Notes |
|---|---|---|
| `DATABASE_URL` | Yes | Neon Postgres connection string (`postgresql://...?sslmode=require`) |
| `OPENAI_API_KEY` | Yes | Tagging is skipped (entries stored with `[]`) if unset |
| `OPENAI_MODEL` | No | Defaults to `gpt-4o-mini` |

## Installing dependencies

```bash
pip install -r rss_fetcher/requirements.txt
```

## Architecture

The service is a single-run Python script (`main.py`) designed to be invoked by an external scheduler (GitHub Actions cron, Railway, Render, etc.) and exit. There is no long-running process.

### Data flow

```
feeds.json → fetcher.py → tagger.py → db.py → Neon Postgres
```

1. **`main.py`** — orchestrator: loads feeds, calls fetcher → tagger → db in sequence per feed. After inserting, queries for any entries with empty tags and auto-backfills them.
2. **`fetcher.py`** — parses RSS/Atom via `feedparser`, normalises entries into flat dicts.
3. **`tagger.py`** — sends entries to OpenAI in batches using **Structured Outputs** (`response_format` with a `json_schema` enum). The enum is built at runtime from `config.json` categories, so invalid tags are impossible at the API level. Retries each batch once on failure. Applies `["Misc"]` as a code-level fallback for empty tag arrays.
4. **`db.py`** — psycopg3 connection to Neon Postgres. `ensure_schema()` is idempotent (runs on every startup). Duplicates are silently skipped via `ON CONFLICT DO NOTHING` keyed on `(feed_url, guid)`.

### Configuration files

- **`rss_fetcher/feeds.json`** — list of `{name, url}` feed objects. Add/remove feeds here.
- **`rss_fetcher/config.json`** — tagging config: `categories` list (drives the JSON Schema enum), `system_prompt` (use `{categories}` placeholder), `batch_size`. Editing this file alone changes tagging behaviour without touching code.

### Database schema

Single table `feed_entries` with columns: `id`, `feed_name`, `feed_url`, `guid`, `title`, `link`, `summary`, `author`, `tags TEXT[]`, `published_at`, `fetched_at`. Unique constraint on `(feed_url, guid)`. GIN index on `tags` for array queries.

### Logging

Logs go to both stdout and `rss_fetcher/logs/rss_fetcher.log` (daily rotation, 7-day retention). The `logs/` directory is gitignored.

### Scheduling

GitHub Actions workflow at `.github/workflows/rss_fetcher.yml` runs on `0 */4 * * *` (every 4 hours). `OPENAI_MODEL` is set via a repository **variable** (`vars.OPENAI_MODEL`); `DATABASE_URL` and `OPENAI_API_KEY` are repository **secrets**.
