"""Settings for the question feature's database access.

Deliberately separate from ingestion/config.py: the web side needs only the
read-only login, never the Discogs token or the writing login's password.
"""
import os

from dotenv import load_dotenv

load_dotenv()

POSTGRES_HOST = os.environ["POSTGRES_HOST"]
POSTGRES_PORT = os.environ["POSTGRES_PORT"]
POSTGRES_DB = os.environ["POSTGRES_DB"]
POSTGRES_READER_USER = os.environ["POSTGRES_READER_USER"]
POSTGRES_READER_PASSWORD = os.environ["POSTGRES_READER_PASSWORD"]
