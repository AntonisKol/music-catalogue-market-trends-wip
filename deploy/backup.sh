#!/bin/sh
# Dump the database to backups/, keep the last 14 days, and copy them to the
# Storage Box named by BACKUP_TARGET in .env (for example
# u123456@u123456.your-storagebox.de:market-trends). Without BACKUP_TARGET the
# dump stays on the server only, and the script says so and fails.
set -eu
cd "$(dirname "$0")/.."
COMPOSE="docker compose --env-file .env -f deploy/compose.yml"
KEEP_DAYS=14

mkdir -p backups
# A run that failed may have left its half-written dump behind. It is no backup, so it
# must not stay here or be copied to the Storage Box.
find backups -name '*.partial' -delete
file="backups/market-trends-$(date -u +%Y-%m-%d).dump"
# Written to a temporary name first, so a dump cut off halfway is never kept as a backup.
$COMPOSE exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' > "$file.partial"
# A dump that pg_restore cannot list is broken.
$COMPOSE exec -T db pg_restore --list < "$file.partial" > /dev/null
mv "$file.partial" "$file"
find backups -name 'market-trends-*.dump' -mtime +$((KEEP_DAYS - 1)) -delete
echo "$(date -u '+%Y-%m-%d %H:%M') saved $file ($(du -h "$file" | cut -f1))"

target=$(sed -n 's/^BACKUP_TARGET=//p' .env | tail -1)
if [ -z "$target" ]; then
  echo "BACKUP_TARGET is not set in .env, so the backup was not copied off the server." >&2
  exit 1
fi
# Storage Boxes take SSH on port 23; the first copy records the box's host key.
# --delete keeps the copy to the same 14 days.
rsync -a --delete -e "ssh -p 23 -o BatchMode=yes -o StrictHostKeyChecking=accept-new" backups/ "$target/"
echo "$(date -u '+%Y-%m-%d %H:%M') copied backups to the Storage Box"
