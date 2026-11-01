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
   - `S3_*`: the object storage bucket and keys, for backups and purchase documents
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
traceback is under Administration → Journaux → Erreurs (super admin only),
searchable by that reference, kept 90 days.

The Activité tab beside it is the audit log: every creation, change,
deletion, pipeline stage, payment, void, import, sign-in and refused sign-in,
with who, when and from which IP. It is never pruned.

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

## Purchase documents

Files attached to purchases (supplier invoice, BL, customs receipt…) are kept:

- in the S3 bucket, under `{S3_PREFIX}documents/<purchase id>/`, when `S3_*`
  is filled in: off the server, so losing the server loses no paper. The
  backup rotation only ever touches `backups/`, never this folder.
- otherwise in the `documents_data` Docker volume (`/app/data/documents` in
  the container). It survives rebuilds, but lives on the server only: fill in
  `S3_*` before the client starts relying on it. To copy it off the server:

  ```bash
  docker run --rm -v fels_web_documents_data:/d -v "$PWD":/out alpine       tar czf /out/documents-$(date +%F).tar.gz -C /d .
  ```

Switching from the volume to S3 later does not move the existing files:
copy the volume's folders into `documents/` in the bucket first.

Accepted: PDF, JPG, PNG, WEBP, HEIC, Word, Excel; 15 MB a file. The types
offered are listed in `models.DOCUMENT_KINDS`, with their names in
`languages.py` (`doc.kind.*`).

## Invoices

Before the first invoice, under Paramètres:

- **Entreprise**: the company name, address, NIF and RCCM printed at the top.
- **Facturation**: whether VAT applies (off until the client confirms), its
  rate (18 % by default), and the footer line (payment terms, bank details).
  Sale prices are what the customer pays: with VAT on, the invoice shows the
  VAT inside the price (total x rate / (100 + rate)), the net and the total.

An issued invoice copies the customer, the lines and the tax of that day; a
later change to the sale does not alter it -- the sale page then warns, and
the invoice is cancelled (with a reason, keeping its number) and reissued.

## Logo and colours

The logo files in `static/images/` were cut from the client's artwork
(`logo.png` for light paper, `logo-on-dark.png` for the sidebar, the F mark
for the icons, `login-illustration.jpg`). With the original vector file from
the client's designer, replace them at the same names. The colours are the
tokens at the top of `static/css/style.css`.

