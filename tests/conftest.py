"""Shared test setup.

ingestion/config.py stops at import when a setting is missing, which is what the
app should do. Tests set obviously fake values here, before any ingestion module
is imported, so they never need or use the real .env. load_dotenv() does not
override variables that are already set.
"""
import os

FAKE_SETTINGS = {
    "DISCOGS_TOKEN": "test-token",
    "DISCOGS_USER_AGENT": "MusicCatalogueMarketTrendsTests/0.1",
    "POSTGRES_HOST": "localhost",
    "POSTGRES_PORT": "5433",
    "POSTGRES_USER": "test",
    "POSTGRES_PASSWORD": "test",
    "POSTGRES_DB": "test",
}

os.environ.update(FAKE_SETTINGS)
