#!/bin/sh
# What cron runs on the server each night: the nightly update, up to ten more techno
# labels while the next night has room (issue 81), then the backup. Each step runs
# even when the one before had problems. Failures go to logs/cron.log.
set -u
cd "$(dirname "$0")/.."
COMPOSE="docker compose --env-file .env -f deploy/compose.yml"

$COMPOSE run --rm -T admin python -m ingestion.nightly
$COMPOSE run --rm -T admin python -m ingestion.grow_labels
if ! sh deploy/backup.sh; then
  echo "$(date -u '+%Y-%m-%d %H:%M') BACKUP FAILED. Rerun it with: sh deploy/backup.sh" >&2
  exit 1
fi
