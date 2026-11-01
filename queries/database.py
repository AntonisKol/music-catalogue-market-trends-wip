import psycopg

from queries import config

# The reader role is SELECT-only and the database already opens its sessions
# read-only with a 5 s limit (see ingestion/create_reader_role.py). The
# connection asks for the same again, as a second lock.
SESSION_OPTIONS = "-c default_transaction_read_only=on -c statement_timeout=5s"


def connect_reader() -> psycopg.Connection:
    return psycopg.connect(
        host=config.POSTGRES_HOST,
        port=config.POSTGRES_PORT,
        user=config.POSTGRES_READER_USER,
        password=config.POSTGRES_READER_PASSWORD,
        dbname=config.POSTGRES_DB,
        options=SESSION_OPTIONS,
        autocommit=True,
    )
