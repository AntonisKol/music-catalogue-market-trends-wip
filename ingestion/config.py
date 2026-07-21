import os
from dotenv import load_dotenv

load_dotenv()

DISCOGS_TOKEN = os.environ["DISCOGS_TOKEN"]
DISCOGS_USER_AGENT = os.environ["DISCOGS_USER_AGENT"]

POSTGRES_HOST = os.environ["POSTGRES_HOST"]
POSTGRES_PORT = os.environ["POSTGRES_PORT"]
POSTGRES_USER = os.environ["POSTGRES_USER"]
POSTGRES_PASSWORD = os.environ["POSTGRES_PASSWORD"]
POSTGRES_DB = os.environ["POSTGRES_DB"]
