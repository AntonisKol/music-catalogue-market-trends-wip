# Music Catalogue Market Trends

Tracks a Discogs label catalogue and how the market moves around it. Release, artist, and label records live in Postgres, alongside daily snapshots of asking price and collector demand (want, have, and rating).

This repo currently has the database and a Discogs API client. Fetching a release or a label's release list works from Python. Writing those responses into the tables is not wired up yet.

## What is stored

| Table | Contents |
| --- | --- |
| `labels` | Discogs label id, name, and resource URL |
| `artists` | Discogs artist id, name, and resource URL |
| `releases` | Release detail: title, year, country, catalogue number, formats, genres, styles, and whether it is physical |
| `release_artists` | Which artists appear on which release |
| `price_snapshots` | One row per release per day: number for sale, lowest price, and currency |
| `demand_snapshots` | One row per release per day: want count, have count, rating count, and average rating |

`labels`, `artists`, and `releases` are the catalogue. `price_snapshots` and `demand_snapshots` are append-only daily facts, so the same release can be compared over time.

## Requirements

- Docker with Compose
- Python 3.9 or newer
- A [Discogs personal access token](https://www.discogs.com/settings/developers)

## Run it locally

From the repo root, create a `.env` file. Compose and the Python client both read it.

```
DISCOGS_TOKEN=your_personal_access_token
DISCOGS_USER_AGENT=MusicCatalogueMarketTrends/0.1 +https://github.com/AntonisKol/music-catalogue-market-trends-wip
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_USER=catalogue
POSTGRES_PASSWORD=catalogue
POSTGRES_DB=catalogue
```

`DISCOGS_USER_AGENT` must identify this app. Discogs rejects generic clients. Use your own token and password; the values above are only a local example.

Start Postgres. On the first start, `db/schema.sql` is applied automatically.

```bash
docker compose up -d
```

Confirm the tables exist:

```bash
docker compose exec db psql -U catalogue -d catalogue -c '\dt'
```

Install the Python dependencies:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Fetch one release. `get_release` returns the full Discogs release, which is what the schema is shaped for. `249504` is the release id from the Discogs API docs; swap in any other id.

```bash
python - <<'PY'
from ingestion.discogs_client import get_release

release = get_release(249504)
print(release["title"], release.get("year"))
PY
```

To collect release ids for a label, call `get_label_releases`. It returns summary records (the list endpoint is not detailed enough to fill `releases` on its own) and pauses between pages so the client stays under Discogs' authenticated limit of 60 requests per minute. Pick a label you actually want; a large catalogue will take a while.

```bash
python - <<'PY'
from ingestion.discogs_client import get_label_releases

summaries = get_label_releases(12345)  # replace with a Discogs label id
print(len(summaries), summaries[0]["id"], summaries[0]["title"])
PY
```

Nothing in these examples writes to Postgres. The database is ready for a later load step.

## Reset the database

Schema changes apply only when the data volume is created. To reapply `db/schema.sql` from scratch:

```bash
docker compose down -v
docker compose up -d
```
