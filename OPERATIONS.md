# Operations

## Deploy on a fresh Linode

1. Point the domain's A record at the server, open ports 80 and 443, install
   Docker with the Compose plugin.
2. Copy the project to the server, for example `/srv/fels_web`.
3. Copy `.env.example` to `.env` and fill in at minimum:
   - `SECRET_KEY`: a long random string
   - `DB_PASSWORD`, and the same value inside `DATABASE_URL`
   - `APP_DOMAIN`: the domain, e.g. `app.example.com`
   - `ADMIN_USERNAME` / `ADMIN_PASSWORD`: used once to create the administrator.
     Change the password after the first sign-in.
   - `SESSION_COOKIE_SECURE=1`
   - `S3_*`: the object storage bucket and keys, for backups
4. Build and start:

   ```bash
   docker compose up -d --build
   ```

On every start, `entrypoint.sh` runs:

```
flask db upgrade   # apply any new migrations
flask seed         # idempotent: settings, ports, administrator
```

`flask seed` never overwrites an existing administrator, so it is safe to
leave `ADMIN_PASSWORD` in `.env` across redeploys.

Then sign in and work through the checklist on the dashboard: the USD→GNF
rate first, then the company details, suppliers, models and customers.

## Redeploy

```bash
cd /srv/fels_web
git pull                       # or copy the new files over
docker compose up -d --build web
```

Migrations run on boot. Static files are cache-busted automatically.

## Importing the client's existing workbook

Administration → Import Excel. Upload the .xlsx; nothing is written until the
preview is confirmed. The preview lists what will be created and what looks
wrong, and asks which catalog model each description of the file stands for
(the sheet's own legend, such as "FT = Flatbed Trailers", is used to suggest
one). Confirming writes everything in one transaction. An invoice already in
the app is skipped, so running the import twice does not duplicate anything.

Before importing: set the USD → GNF rate and create the catalog models (or
create them from the preview). After importing: the purchases' stages, the
sales' vehicles and any placeholder amounts are corrected in the app.

The uploaded file is kept in the container's temp folder only until the import
is confirmed or abandoned, and is never served.

## Administrator

### Bootstrap without a password in .env

Leave `ADMIN_PASSWORD` empty and create the account from the CLI:

```bash
docker compose exec web flask seed-super-admin \
  --username admin --password 'one-time-temporary-password'
```

### Reset the administrator's password

```bash
docker compose exec web flask reset-super-admin-password --password 'new-pw'
```

## Errors

An unexpected error shows the user a reference such as `ERR-7F3A2C`. The full
traceback is under Administration → Journal des erreurs, searchable by that
reference, kept 90 days.

```bash
docker compose logs -f web                  # follow the app's log
docker compose logs --tail=200 web | grep ERR-
```

## Database backups

Everything the business records lives in one Postgres volume on one server.
The volume survives a redeploy; it does not survive a failed disk, a
`docker compose down -v`, or the server being lost. These backups are the copy
that lives somewhere else.

### What happens

A dump is taken, gzipped and sent to object storage, into a `backups/` folder
grouped by month. It never touches the server's disk: it is piped from the
database container straight to the upload.

Kept: **every dump of the last 30 days**, then **the first dump of each month
for a year**. Older ones are deleted. A file whose name the rotation cannot
read is never deleted.

### Set it up

With `S3_*` filled in `.env`, once, on the server:

```bash
crontab -e
```

and add, adjusting the path:

```cron
# Backup fels_web every night at 03:00
0 3 * * * cd /srv/fels_web && ./scripts/backup-db.sh >> /var/log/fels-backup.log 2>&1
```

### Check it

```bash
./scripts/backup-db.sh                                     # run one now
docker compose exec -T web python scripts/backup_db.py --list
tail -20 /var/log/fels-backup.log
```

The script refuses to upload anything too small to be a real dump: a failed
`pg_dump` still produces a valid, tiny gzip, and replacing a good backup with
one of those is the one way this could fail quietly.

### Restore

**This replaces everything in the database.** It asks for the database name
before doing anything, and it keeps the current database aside first, in
`backup-before-restore-<date>.sql.gz`, in case the wrong backup was chosen.

```bash
./scripts/restore-db.sh                                    # list what is there
./scripts/restore-db.sh backups/2026-11/fels-2026-11-02-0300.sql.gz
```

Afterwards, check the app opens and the figures look right, then delete the
safety file.

### Thin the folder without taking a backup

```bash
docker compose exec -T web python scripts/backup_db.py --rotate-only --dry-run
docker compose exec -T web python scripts/backup_db.py --rotate-only
```
