#!/bin/sh
# Load a database dump into the server's database, replacing what is there.
#   sh deploy/restore.sh backups/market-trends-2026-11-03.dump
# Used once to move the Mac's data to the server, and to recover from a backup.
# Afterwards the reader and web logins are set up again, because a dump does not
# carry logins or their rights.
set -eu
cd "$(dirname "$0")/.."
COMPOSE="docker compose --env-file .env -f deploy/compose.yml"

if [ $# -ne 1 ] || [ ! -f "$1" ]; then
  echo "Usage: sh deploy/restore.sh <file.dump>" >&2
  exit 2
fi
printf 'This replaces everything in the server database with %s. Type yes to go on: ' "$1"
read -r answer
[ "$answer" = "yes" ] || { echo "Nothing changed."; exit 1; }

$COMPOSE up -d db
until $COMPOSE exec -T db sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"' > /dev/null 2>&1; do sleep 1; done
$COMPOSE stop api
$COMPOSE exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --no-privileges --single-transaction' < "$1"
$COMPOSE run --rm -T api python -m ingestion.apply_schema
$COMPOSE run --rm -T api python -m ingestion.create_reader_role
$COMPOSE run --rm -T api python -m ingestion.create_limits_role
$COMPOSE up -d
echo "Restored $1."
