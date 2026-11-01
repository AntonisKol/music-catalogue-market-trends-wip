import psycopg

from ingestion import config


def connect() -> psycopg.Connection:
    return psycopg.connect(
        host=config.POSTGRES_HOST,
        port=config.POSTGRES_PORT,
        user=config.POSTGRES_USER,
        password=config.POSTGRES_PASSWORD,
        dbname=config.POSTGRES_DB,
    )
