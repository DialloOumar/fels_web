"""Where the files attached to purchases are kept.

With S3_* set, in the object storage bucket the backups use, under its own
documents/ folder: off the server, so a lost server loses no paper. Without
it, in DOCUMENTS_DIR on the server -- a Docker volume, kept across rebuilds.
The database row holds the key; this module holds the bytes.
"""
import logging
import os
import uuid

import s3_storage

log = logging.getLogger(__name__)

DOCUMENTS_DIR = os.environ.get("DOCUMENTS_DIR", "/app/data/documents")

# What may be attached, and the type each is served as. The type comes from
# the extension, never from what the browser claims.
ALLOWED = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
    ".heic": "image/heic",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
# Opened in the browser; anything else downloads.
SHOWN_INLINE = {"application/pdf", "image/jpeg", "image/png", "image/webp"}
MAX_BYTES = 15 * 1024 * 1024


def new_key(purchase_id, ext):
    return "%d/%s%s" % (purchase_id, uuid.uuid4().hex, ext)


def _path(key):
    path = os.path.normpath(os.path.join(DOCUMENTS_DIR, key))
    if not path.startswith(os.path.normpath(DOCUMENTS_DIR) + os.sep):
        raise ValueError("bad key")
    return path


def save(key, data, content_type):
    """Store the bytes. Returns True when they are safely kept."""
    if s3_storage.is_configured():
        return s3_storage.put_document(key, data, content_type) is None
    try:
        path = _path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return True
    except OSError:
        log.exception("could not store document %s", key)
        return False


def load(key):
    """The bytes, or None when the file cannot be found."""
    if s3_storage.is_configured():
        return s3_storage.get_document(key)
    try:
        with open(_path(key), "rb") as fh:
            return fh.read()
    except OSError:
        log.exception("could not read document %s", key)
        return None


def remove(key):
    """Best effort: a file left behind costs a little space, never data."""
    if s3_storage.is_configured():
        return s3_storage.delete_document(key)
    try:
        os.remove(_path(key))
        return True
    except OSError:
        return False
