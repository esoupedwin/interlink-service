# INTERLINK

A scheduled news service that automatically aggregates RSS feeds from major global outlets, translates non-English content, categorises each article using OpenAI's LLM, scrapes the full article body to generate a concise gist, and stores everything in a Neon Postgres database.

Every 4 hours it:
1. Fetches new entries from configured RSS feeds
2. Detects non-English titles/summaries and translates them to English via OpenAI, preserving originals in `original_title`/`original_summary`
3. Tags each article with **geo tags** (geopolitical regions) and **topic tags** using OpenAI Structured Outputs — invalid tags are impossible at the API level
4. Scrapes the full article text and generates a 2–3 sentence English gist via LLM; promotional content and non-articles are detected and left with a null gist
5. Inserts only new articles — duplicates are silently skipped

The result is a continuously updated, machine-tagged news database ready to be queried or served to downstream applications.

## Feeds

| Feed | Source |
|---|---|
| CNN News | `http://rss.cnn.com/rss/edition.rss` |
| BBC Feed | `http://feeds.bbci.co.uk/news/rss.xml` |
| Al Jazeera Feed | `https://www.aljazeera.com/xml/rss/all.xml` |
| China News | `https://www.chinanews.com.cn/rss/china.xml` |
| South China Morning Post | `https://www.scmp.com/rss/4/feed/` |
| The Verge | `https://www.theverge.com/rss/index.xml` |
| TechCrunch (AI) | `https://techcrunch.com/tag/artificial-intelligence/feed/` |
| MIT Technology Review (AI) | `https://www.technologyreview.com/topic/artificial-intelligence/feed/` |
| Straits Times (Singapore) | `https://www.straitstimes.com/news/singapore/rss.xml` |

Add or remove feeds in `rss_fetcher/feeds.json`.

## Database schema

Single table `feed_entries`:

| Column | Type | Notes |
|---|---|---|
| `id` | BIGSERIAL | Primary key |
| `feed_name` | TEXT | Human-readable feed name |
| `feed_url` | TEXT | Feed source URL |
| `guid` | TEXT | RSS entry GUID; unique per feed |
| `title` | TEXT | Article title (English) |
| `original_title` | TEXT | Pre-translation title; null for English-source entries |
| `link` | TEXT | Article URL |
| `summary` | TEXT | RSS-provided summary (English) |
| `original_summary` | TEXT | Pre-translation summary; null for English-source entries |
| `author` | TEXT | |
| `geo_tags` | TEXT[] | LLM-assigned geographic tags; GIN indexed |
| `topic_tags` | TEXT[] | LLM-assigned topic tags; GIN indexed |
| `gist` | TEXT | LLM-generated 2–3 sentence English summary; null for non-articles or Misc-tagged entries |
| `published_at` | TIMESTAMPTZ | |
| `fetched_at` | TIMESTAMPTZ | Insertion time |

Unique constraint on `(feed_url, guid)`.

## Tag categories

### Geographic tags
`United States`, `China`, `Russia`, `Taiwan`, `Middle East`, `Europe`, `Southeast Asia`, `Africa`, `Latin America`, `Japan`, `India`, `North Korea`, `South Korea`, `Australia`, `Israel`, `Iran`, `Ukraine`, `Singapore`, `Transnational`, `Others`

> **Transnational** is used for articles about multinational corporations or global tech companies (e.g. NVIDIA, Meta, Google) not tied to a specific country.

### Topic tags
`Domestic Politics`, `Bilateral Relations`, `Military`, `Economy Trade`, `Cybersecurity`, `Energy`, `Climate`, `AI`, `Space`, `Belt And Road`, `Misc`

> Articles tagged **Misc** are skipped by the summariser and receive no gist.

## Configuration

- **`rss_fetcher/feeds.json`** — list of `{name, url}` feed objects
- **`rss_fetcher/config.json`** — tag categories, system prompts, batch sizes, and retry settings for all pipeline stages

## Environment variables

| Variable | Required | Notes |
|---|---|---|
| `DATABASE_URL` | Yes | Neon Postgres connection string (`postgresql://...?sslmode=require`) |
| `OPENAI_API_KEY` | Yes | Translation, tagging, and summarisation are skipped if unset |
| `OPENAI_MODEL` | No | Defaults to `gpt-4o-mini` |

## Running locally

```bash
pip install -r rss_fetcher/requirements.txt
```

Create `rss_fetcher/.env` with your credentials:

```
DATABASE_URL=postgresql://...?sslmode=require
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
```

Then run:

```bash
cd rss_fetcher
python main.py
```

To re-tag all existing entries with empty tags:

```bash
cd rss_fetcher
python backfill_tags.py
```

## Scheduling

GitHub Actions workflow (`.github/workflows/rss_fetcher.yml`) runs on `0 */4 * * *`. `DATABASE_URL` and `OPENAI_API_KEY` are repository secrets; `OPENAI_MODEL` is a repository variable.
