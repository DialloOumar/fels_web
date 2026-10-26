#!/bin/bash
#
# Put a backup back. This REPLACES everything currently in the database.
#
#   ./scripts/restore-db.sh                       # list what is available
#   ./scripts/restore-db.sh backups/2026-09/fels-2026-09-12-0300.sql.gz
#
# It asks before doing anything, and it says what it is about to destroy. A
# restore is the one command here that cannot be undone, so it is deliberately
# slower to run than the backup is.
set -euo pipefail

cd "$(dirname "$0")/.."

DB_USER="${POSTGRES_USER:-fels_user}"
DB_NAME="${POSTGRES_DB:-fels}"
KEY="${1:-}"

if [ -z "$KEY" ]; then
    echo "Available backups:"
    echo
    docker compose exec -T web python scripts/backup_db.py --list
    echo
    echo "Then: $0 <key shown above>"
    exit 0
fi

echo "About to restore:  $KEY"
echo "Into database:     $DB_NAME"
echo
echo "EVERYTHING currently in that database will be replaced — every purchase,"
echo "sale, payment, customer and supplier recorded since that backup was taken."
echo
read -rp "Type the database name ($DB_NAME) to confirm: " CONFIRM
if [ "$CONFIRM" != "$DB_NAME" ]; then
    echo "Not confirmed. Nothing was touched."
    exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
LOCAL="$TMP/restore.sql.gz"

echo "Fetching…"
docker compose exec -T web python scripts/backup_db.py --fetch "$KEY" > "$LOCAL"

if [ ! -s "$LOCAL" ]; then
    echo "Nothing came back. Nothing was touched."
    exit 1
fi
echo "Fetched $(du -h "$LOCAL" | cut -f1)."

# A safety net for the restore itself: whatever is there now, kept aside, in
# case the backup turns out to be the wrong one.
SAFETY="./backup-before-restore-$(date '+%Y%m%d-%H%M%S').sql.gz"
echo "Keeping the current database aside in $SAFETY …"
docker compose exec -T db pg_dump -U "$DB_USER" -d "$DB_NAME" | gzip -9 > "$SAFETY"

echo "Restoring…"
gunzip -c "$LOCAL" | docker compose exec -T db psql -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 --quiet

echo
echo "Restored. The database as it was a minute ago is in $SAFETY —"
echo "delete it once you are satisfied."
