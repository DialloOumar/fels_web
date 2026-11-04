"""The photos of a model, made fit for the screen when they are uploaded.

A phone photo is 3 to 8 MB and sideways as often as not. Each one is turned
upright from its EXIF, made RGB, and kept twice as JPEG: a large copy to
open (1600 px on its long side) and a thumbnail for the lists (480 px).
The bytes live in documents_store, under products/; the rows say where.
"""
import io
import uuid

from PIL import Image, ImageOps, UnidentifiedImageError

try:                                    # iPhone photos (HEIC)
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:                     # pragma: no cover -- the image has it
    pass

import documents_store

MAX_UPLOAD = 20 * 1024 * 1024
LARGE, THUMB = 1600, 480
ACCEPT = "image/*,.heic,.heif"


def _jpeg(img, side):
    copy = img.copy()
    copy.thumbnail((side, side), Image.LANCZOS)
    out = io.BytesIO()
    copy.save(out, "JPEG", quality=85, optimize=True, progressive=True)
    return out.getvalue()


def prepare(data):
    """(large JPEG, thumbnail JPEG) from uploaded bytes, or None when they
    are not a picture Pillow can read."""
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            white = Image.new("RGB", img.size, (255, 255, 255))
            white.paste(img, mask=img.split()[-1])
            img = white
        else:
            img = img.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError):
        return None
    return _jpeg(img, LARGE), _jpeg(img, THUMB)


def store(product_id, data):
    """Keep a photo's two copies. Returns (key, thumb_key) or None when the
    storage refused them."""
    stem = "products/%d/%s" % (product_id, uuid.uuid4().hex)
    large, thumb = data
    key, thumb_key = stem + ".jpg", stem + "-thumb.jpg"
    if not documents_store.save(key, large, "image/jpeg"):
        return None
    if not documents_store.save(thumb_key, thumb, "image/jpeg"):
        documents_store.remove(key)
        return None
    return key, thumb_key


def remove(photo):
    documents_store.remove(photo.key)
    documents_store.remove(photo.thumb_key)
