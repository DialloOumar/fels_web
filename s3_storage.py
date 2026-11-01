"""Linode Object Storage helpers for database backups.

Everything here is best-effort: failures are logged and reported as an error
code, never raised, so a storage hiccup cannot take the app down. Without
the S3_* settings the module loads and simply reports "not configured".
"""
import logging
import os
from typing import Optional, Tuple

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:  # pragma: no cover - only where boto3 is absent
    boto3 = None
    class BotoCoreError(Exception): pass
    class ClientError(Exception): pass

ERR_NOT_CONFIGURED = "not_configured"
ERR_S3             = "s3_error"
ERR_UNKNOWN        = "unknown"

log = logging.getLogger(__name__)

S3_BUCKET       = os.environ.get("S3_BUCKET", "").strip()
S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL", "").strip()
# boto3 wants a full URL; "fr-par-1.linodeobjects.com", as Linode's console
# shows it, is taken to mean https.
if S3_ENDPOINT_URL and "://" not in S3_ENDPOINT_URL:
    S3_ENDPOINT_URL = "https://" + S3_ENDPOINT_URL
# The bucket's own address (felsstorage.fr-par-1.linodeobjects.com) is not
# the endpoint: boto3 adds the bucket itself. Keep the region's address.
if S3_BUCKET and "://%s." % S3_BUCKET in S3_ENDPOINT_URL:
    S3_ENDPOINT_URL = S3_ENDPOINT_URL.replace("://%s." % S3_BUCKET, "://", 1)
S3_ENDPOINT_URL = S3_ENDPOINT_URL.rstrip("/")
S3_REGION       = os.environ.get("S3_REGION", "")
S3_ACCESS_KEY   = os.environ.get("S3_ACCESS_KEY", "")
S3_SECRET_KEY   = os.environ.get("S3_SECRET_KEY", "")
# Per-environment folder inside the bucket (e.g. "prod/"). Empty: bucket root.
S3_PREFIX       = os.environ.get("S3_PREFIX", "")
if S3_PREFIX and not S3_PREFIX.endswith("/"):
    S3_PREFIX += "/"

BACKUP_PREFIX = S3_PREFIX + "backups/"       # database dumps, and nothing else
DOCUMENT_PREFIX = S3_PREFIX + "documents/"   # files attached to purchases


def _client():
    """Lazily build an S3 client; None if not configured."""
    if boto3 is None:
        return None
    if not all([S3_ENDPOINT_URL, S3_BUCKET, S3_ACCESS_KEY, S3_SECRET_KEY]):
        return None
    try:
        return boto3.client(
            "s3",
            endpoint_url=S3_ENDPOINT_URL,
            region_name=S3_REGION or None,
            aws_access_key_id=S3_ACCESS_KEY,
            aws_secret_access_key=S3_SECRET_KEY,
        )
    except (ValueError, BotoCoreError):
        # A malformed setting reads as "not configured", logged, rather than
        # an error page on every screen that touches storage.
        log.exception("S3 settings rejected (S3_ENDPOINT_URL=%r)", S3_ENDPOINT_URL)
        return None


def is_configured() -> bool:
    return _client() is not None


def upload_backup(stream, month: str, name: str) -> Tuple[Optional[str], Optional[str]]:
    """Send a dump to {S3_PREFIX}backups/<month>/<name>, the month being
    "YYYY-MM". `stream` is read in chunks, so a large dump never has to be held
    in memory at once.

    Returns (key, None) or (None, error_code).
    """
    s3 = _client()
    if s3 is None:
        return None, ERR_NOT_CONFIGURED
    key = BACKUP_PREFIX + (month + "/" if month else "") + name
    try:
        s3.upload_fileobj(stream, S3_BUCKET, key)
        return key, None
    except (BotoCoreError, ClientError) as e:
        log.exception("Backup upload failed: %s", e)
        return None, ERR_S3
    except Exception as e:
        log.exception("Unexpected error during backup upload: %s", e)
        return None, ERR_UNKNOWN


def list_backups():
    """Every dump in the bucket, newest first: [(key, size, last_modified)]."""
    s3 = _client()
    if s3 is None:
        return []
    out, token = [], None
    while True:
        kw = {"Bucket": S3_BUCKET, "Prefix": BACKUP_PREFIX}
        if token:
            kw["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kw)
        for obj in resp.get("Contents", []):
            out.append((obj["Key"], obj["Size"], obj["LastModified"]))
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    out.sort(key=lambda r: r[0], reverse=True)
    return out


def download_backup(key: str, dest_path: str) -> Optional[str]:
    """Fetch one dump to a local file. Returns an error code, or None on success."""
    s3 = _client()
    if s3 is None:
        return ERR_NOT_CONFIGURED
    try:
        with open(dest_path, "wb") as fh:
            s3.download_fileobj(S3_BUCKET, key, fh)
        return None
    except (BotoCoreError, ClientError) as e:
        log.exception("Backup download failed: %s", e)
        return ERR_S3


def delete_backup(key: str) -> bool:
    """Remove one dump. Used by the rotation, never by the app."""
    s3 = _client()
    if s3 is None or not key.startswith(BACKUP_PREFIX):
        # Refusing a key outside the backups folder is the whole guard: the
        # rotation must never be able to reach a photo.
        return False
    try:
        s3.delete_object(Bucket=S3_BUCKET, Key=key)
        return True
    except (BotoCoreError, ClientError) as e:
        log.exception("Backup delete failed: %s", e)
        return False


# ── Purchase documents ───────────────────────────────────────────────────────
# Same bucket, their own folder. Unlike a backup, a document the user just
# attached must not vanish quietly: each call says whether it worked.


def put_document(key: str, data: bytes, content_type: str) -> Optional[str]:
    """Store one document under {S3_PREFIX}documents/<key>. Returns an
    error code, or None on success."""
    s3 = _client()
    if s3 is None:
        return ERR_NOT_CONFIGURED
    try:
        s3.put_object(Bucket=S3_BUCKET, Key=DOCUMENT_PREFIX + key, Body=data, ContentType=content_type)
        return None
    except (BotoCoreError, ClientError) as e:
        log.exception("Document upload failed: %s", e)
        return ERR_S3


def get_document(key: str) -> Optional[bytes]:
    """One document's bytes, or None if it cannot be read."""
    s3 = _client()
    if s3 is None:
        return None
    try:
        return s3.get_object(Bucket=S3_BUCKET, Key=DOCUMENT_PREFIX + key)["Body"].read()
    except (BotoCoreError, ClientError) as e:
        log.exception("Document download failed: %s", e)
        return None


def delete_document(key: str) -> bool:
    s3 = _client()
    if s3 is None:
        return False
    try:
        s3.delete_object(Bucket=S3_BUCKET, Key=DOCUMENT_PREFIX + key)
        return True
    except (BotoCoreError, ClientError) as e:
        log.exception("Document delete failed: %s", e)
        return False
