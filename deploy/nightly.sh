#!/bin/sh
# What cron runs on the server each night: the nightly update, then the backup.
# The backup runs even when the update had problems. Failures go to logs/cron.log.
set -u
cd "$(dirname "$0")/.."
COMPOSE="docker compose --env-file .env -f deploy/compose.yml"

$COMPOSE run --rm -T admin python -m ingestion.nightly
if ! sh deploy/backup.sh; then
  echo "$(date -u '+%Y-%m-%d %H:%M') BACKUP FAILED. Rerun it with: sh deploy/backup.sh" >&2
  exit 1
fi
