# -*- coding: utf-8 -*-
"""Send a database dump to object storage, and thin out the old ones.

Reads the gzipped dump on standard input, so the dump is never written to the
server's disk: pg_dump runs in the database container, gzip squeezes it, and
this uploads the stream as it arrives. One pipeline, nothing left behind.

    docker compose exec -T db pg_dump ... | gzip -9 | \
        docker compose exec -T web python scripts/backup_db.py

Run it with --list to see what is stored, and --rotate-only to thin the folder
without taking a new dump.

Why it lives in the web container: the S3 credentials, the bucket and the
folder layout are already there, in s3_storage. A second copy of any of that
would be a second thing to keep in step.
"""
import argparse
import io
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import s3_storage  # noqa: E402

# A dump smaller than this is not a dump. pg_dump failing mid-pipe still leaves
# gzip producing a valid, tiny file, and uploading that would replace a real
# backup with a comforting lie -- the one failure mode that matters here, since
# nobody looks at a backup until the day they need it.
MIN_BYTES = 2048

# What is kept, and for how long.
KEEP_DAILY_DAYS = 30      # every dump of the last month
KEEP_MONTHLY = 12         # then the first of each month, for a year

NAME = re.compile(r"fels-(\d{4})-(\d{2})-(\d{2})-(\d{4})\.sql\.gz$")


def _stamp(now=None):
    now = now or datetime.now()
    return now.strftime("%Y-%m-%d-%H%M"), now.strftime("%Y-%m")


def _dump_date(key):
    """The day a stored dump is of, read from its name rather than from the
    bucket's own timestamp — a re-uploaded file would carry today's."""
    m = NAME.search(key)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def plan_rotation(keys, today=None):
    """Which stored dumps to drop. Returns (keep, drop), both lists of keys.

    Everything from the last 30 days stays. Older than that, the first dump of
    each month stays for a year and the rest go. Anything whose name cannot be
    read is kept: a file this cannot account for is not a file it may delete.
    """
    today = today or date.today()
    daily_floor = today - timedelta(days=KEEP_DAILY_DAYS)
    keep, drop = [], []
    # The oldest dump of each month is the one that represents it.
    first_of_month = {}
    for key in keys:
        d = _dump_date(key)
        if d is None:
            continue
        month = (d.year, d.month)
        if month not in first_of_month or key < first_of_month[month]:
            first_of_month[month] = key

    # The first day of the month KEEP_MONTHLY months back, counted in months
    # rather than in days so a year means a year whatever the month lengths.
    months = today.year * 12 + (today.month - 1) - KEEP_MONTHLY
    monthly_floor = date(months // 12, months % 12 + 1, 1)

    for key in keys:
        d = _dump_date(key)
        if d is None or d > daily_floor:
            keep.append(key)
            continue
        if first_of_month.get((d.year, d.month)) == key and d >= monthly_floor:
            keep.append(key)
            continue
        drop.append(key)
    return keep, drop


def do_rotate(dry_run=False):
    stored = [k for k, _size, _mod in s3_storage.list_backups()]
    keep, drop = plan_rotation(stored)
    for key in drop:
        if dry_run:
            print("would delete %s" % key)
        elif s3_storage.delete_backup(key):
            print("deleted %s" % key)
        else:
            print("COULD NOT DELETE %s" % key, file=sys.stderr)
    return len(keep), len(drop)


def do_list():
    rows = s3_storage.list_backups()
    if not rows:
        print("No backup stored yet.")
        return 0
    total = 0
    for key, size, modified in rows:
        total += size
        print("%-58s %9.2f MB  %s" % (key, size / 1048576.0,
                                      modified.strftime("%Y-%m-%d %H:%M")))
    print("\n%d backups, %.1f MB in total." % (len(rows), total / 1048576.0))
    return len(rows)


def do_fetch(key):
    """Write one stored dump to standard output, so it can be redirected to a
    file or piped straight into psql. Nothing else is printed on stdout —
    anything mixed in would corrupt the archive."""
    if not s3_storage.is_configured():
        print("Object storage is not configured.", file=sys.stderr)
        return 2
    fd, tmp = tempfile.mkstemp(suffix=".sql.gz")
    os.close(fd)
    err = s3_storage.download_backup(key, tmp)
    if err:
        print("Download failed: %s" % err, file=sys.stderr)
        return 1
    try:
        with open(tmp, "rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return 0


def do_upload():
    if not s3_storage.is_configured():
        print("Object storage is not configured: nothing to upload to.",
              file=sys.stderr)
        return 2

    # Read the stream once, into memory, so its size can be checked before any
    # of it is sent. These dumps are a few megabytes; the day that stops being
    # true is the day to stream and verify differently.
    payload = sys.stdin.buffer.read()
    if len(payload) < MIN_BYTES:
        print("Refusing to upload: the dump is %d bytes, which means pg_dump "
              "failed. The previous backups are untouched." % len(payload),
              file=sys.stderr)
        return 1
    if payload[:2] != b"\x1f\x8b":
        print("Refusing to upload: this is not gzip data.", file=sys.stderr)
        return 1

    stamp, month = _stamp()
    name = "fels-%s.sql.gz" % stamp
    key, err = s3_storage.upload_backup(io.BytesIO(payload), month, name)
    if err:
        print("Upload failed (%s). The previous backups are untouched." % err,
              file=sys.stderr)
        return 1
    print("Uploaded %s (%.2f MB)" % (key, len(payload) / 1048576.0))

    kept, dropped = do_rotate()
    print("Rotation: %d kept, %d removed." % (kept, dropped))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="show what is stored")
    ap.add_argument("--rotate-only", action="store_true",
                    help="thin the folder without taking a new dump")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --rotate-only, say what would go without going")
    ap.add_argument("--fetch", metavar="KEY",
                    help="write one stored dump to standard output")
    args = ap.parse_args()

    if args.fetch:
        return do_fetch(args.fetch)
    if args.list:
        do_list()
        return 0
    if args.rotate_only:
        kept, dropped = do_rotate(dry_run=args.dry_run)
        print("Rotation: %d kept, %d %s." %
              (kept, dropped, "would be removed" if args.dry_run else "removed"))
        return 0
    return do_upload()


if __name__ == "__main__":
    sys.exit(main())
