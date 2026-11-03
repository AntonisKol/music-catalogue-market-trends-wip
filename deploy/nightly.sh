#!/bin/sh
# What cron runs on the server each night: the nightly update, then the backup.
# The backup runs even when the update had problems, and a failed backup sends an alert.
set -u
cd "$(dirname "$0")/.."
COMPOSE="docker compose --env-file .env -f deploy/compose.yml"

$COMPOSE run --rm -T api python -m ingestion.nightly
if ! sh deploy/backup.sh; then
  $COMPOSE run --rm -T api python -c 'from ingestion.alerts import send_alert; send_alert("Market Trends: backup failed", "The nightly database backup on the server failed; see logs/cron.log there. Rerun it with: sh deploy/backup.sh")'
  exit 1
fi
