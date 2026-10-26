#!/bin/bash
#
# Take a database dump and send it to object storage. This is what cron runs.
#
#   cd /path/to/fels_web && ./scripts/backup-db.sh
#
# The dump never touches the server's disk: pg_dump runs inside the database
# container, gzip squeezes it, and the web container uploads the stream. One
# pipeline, nothing left behind to clean up or to leak.
#
# pipefail is the important line. Without it, pg_dump failing halfway leaves
# gzip finishing happily and the whole pipeline reporting success -- which is
# how a folder fills with empty files that nobody looks at until the day they
# need one. With it, the failure travels, and backup_db.py refuses anything
# too small to be real anyway.
set -euo pipefail

cd "$(dirname "$0")/.."

DB_USER="${POSTGRES_USER:-fels_user}"
DB_NAME="${POSTGRES_DB:-fels}"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Dumping ${DB_NAME}…"

docker compose exec -T db pg_dump -U "$DB_USER" -d "$DB_NAME" \
  | gzip -9 \
  | docker compose exec -T web python scripts/backup_db.py

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Done."
