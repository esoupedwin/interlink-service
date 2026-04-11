# INTERLINK

A scheduled news service that automatically aggregates RSS feeds from major global outlets (CNN, BBC, Al Jazeera, Xinhua, China Daily), categorizes each article using OpenAI's LLM, and stores the results in a Neon Postgres database.

Every 4 hours it fetches new entries, tags them against a predefined category taxonomy (geopolitical regions, topics, conflict types) using Structured Outputs to enforce strict tag validity, and inserts only new articles — making it a continuously updated, machine-tagged news feed database ready to be queried or served to downstream applications.
