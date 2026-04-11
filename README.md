# INTERLINK

A scheduled news intelligence service that automatically aggregates RSS feeds from major global outlets, categorises each article using OpenAI's LLM, scrapes the full article body to generate a concise gist, and stores everything in a Neon Postgres database.

Every 4 hours it:
1. Fetches new entries from configured RSS feeds
2. Tags each article against a predefined category taxonomy (geopolitical regions, topics, conflict types) using OpenAI Structured Outputs — invalid tags are impossible at the API level
3. Scrapes the full article text and generates a 2–3 sentence gist via LLM; promotional content and non-articles are detected and left with a null gist
4. Inserts only new articles — duplicates are silently skipped

The result is a continuously updated, machine-tagged news database ready to be queried or served to downstream applications.

## Feeds

| Feed | Source |
|---|---|
| CNN News | `http://rss.cnn.com/rss/edition.rss` |
| BBC Feed | `http://feeds.bbci.co.uk/news/rss.xml` |
| Al Jazeera Feed | `https://www.aljazeera.com/xml/rss/all.xml` |
| Xinhua China Feed | `https://www.xinhuanet.com/english/rss/chinarss.xml` |
| China Daily Feed | `http://www.chinadaily.com.cn/rss/china_rss.xml` |

Add or remove feeds in `rss_fetcher/feeds.json`.

## Database schema

Single table `feed_entries`:

| Column | Type | Notes |
|---|---|---|
| `id` | BIGSERIAL | Primary key |
| `feed_name` | TEXT | Human-readable feed name |
| `feed_url` | TEXT | Feed source URL |
| `guid` | TEXT | RSS entry GUID; unique per feed |
| `title` | TEXT | Article title |
| `link` | TEXT | Article URL |
| `summary` | TEXT | RSS-provided summary |
| `author` | TEXT | |
| `tags` | TEXT[] | LLM-assigned category tags; GIN indexed |
| `gist` | TEXT | LLM-generated 2–3 sentence summary; null for non-articles or Misc-tagged entries |
| `published_at` | TIMESTAMPTZ | |
| `fetched_at` | TIMESTAMPTZ | Insertion time |

Unique constraint on `(feed_url, guid)`.

## Configuration

- **`rss_fetcher/feeds.json`** — list of `{name, url}` feed objects
- **`rss_fetcher/config.json`** — tagging config: `categories` list, `system_prompt`, `batch_size`

## Environment variables

| Variable | Required | Notes |
|---|---|---|
| `DATABASE_URL` | Yes | Neon Postgres connection string |
| `OPENAI_API_KEY` | Yes | Tagging and summarisation skipped if unset |
| `OPENAI_MODEL` | No | Defaults to `gpt-4o-mini` |

## Running locally

```bash
pip install -r rss_fetcher/requirements.txt
cp rss_fetcher/.env.example rss_fetcher/.env  # fill in DATABASE_URL and OPENAI_API_KEY
cd rss_fetcher
python main.py
```

To re-tag all existing entries with empty tags:

```bash
python rss_fetcher/backfill_tags.py
```

## Scheduling

GitHub Actions workflow (`.github/workflows/rss_fetcher.yml`) runs on `0 */4 * * *`. `DATABASE_URL` and `OPENAI_API_KEY` are repository secrets; `OPENAI_MODEL` is a repository variable.
