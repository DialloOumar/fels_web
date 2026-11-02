"""A signature drawn on white paper, photographed, turned into ink on glass.

The person signs on a blank sheet and uploads the picture. What comes back
is a small transparent PNG holding only the strokes: the paper, the shadows
and the lighting are gone, so the signature can be laid over the company
stamp and printed on a receipt as if it had been signed there.

How: the picture is turned to greys. The paper's own brightness is found
area by area -- the strokes are wiped off with a morphological closing on a
reduced copy, which keeps large dark areas (a table at the edge) and drops
thin ones (the pen) -- so a shadow across the sheet, a dim corner or a lamp
on one side no longer count as ink. What is darker than the paper around it
is ink, and how much darker sets how opaque the pixel is, so thin strokes
keep soft edges. The result is cleaned of specks, cropped to the strokes
and scaled to a printable width.

Accepted: any photo a phone takes (JPEG, PNG, WebP, and iPhone HEIC when
pillow-heif is installed), up to MAX_BYTES; a PNG with a transparent
background is read as strokes on white.
"""
import io

from PIL import Image, ImageChops, ImageFilter, ImageOps

try:                                   # iPhone photos (HEIC / HEIF)
    from pillow_heif import register_heif_opener
    register_heif_opener()
except Exception:                      # pragma: no cover - optional
    pass

INK = (43, 63, 158)      # the stamp's blue, #2B3F9E
MAX_WIDTH = 900
MAX_BYTES = 15 * 1024 * 1024           # under the app's 16 MB request limit
WORK_WIDTH = 1600                      # the picture is read at this width


def _to_grey(img):
    """Greys, with a transparent background read as white paper."""
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        img = img.convert("RGBA")
        white = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(white, img)
    return img.convert("L")


def _paper(grey):
    """The paper's brightness behind every pixel, the strokes wiped off.

    On a copy an eighth of the size, a closing (brightest then darkest of
    each neighbourhood) fills in anything thin and dark -- the pen -- and
    leaves what is wide -- a shadow, a table past the sheet's edge. Blurred
    and brought back to full size, it is the sheet as it was lit."""
    small_w = max(1, grey.width // 8)
    small_h = max(1, grey.height // 8)
    small = grey.resize((small_w, small_h), Image.BILINEAR)
    k = 9                                   # ~70 px at full width: wider than any pen
    small = small.filter(ImageFilter.MaxFilter(k)).filter(ImageFilter.MinFilter(k))
    small = small.filter(ImageFilter.GaussianBlur(2))
    paper = small.resize(grey.size, Image.BILINEAR)
    # Off the sheet: where what lies behind is much darker than the sheet
    # itself (a table, a hand), grown a little so the sheet's edge goes too.
    hist = small.histogram()
    sheet = 255 - _level([hist[255 - i] for i in range(256)], sum(hist), 0.5)   # median brightness
    off = small.filter(ImageFilter.MinFilter(5)).point(lambda v: 255 if v < sheet * 0.75 else 0)
    return paper, off.resize(grey.size, Image.NEAREST)


def _level(hist, total, share):
    """The darkness level reached by the darkest `share` of the pixels."""
    cum = 0
    for level in range(255, -1, -1):
        cum += hist[level]
        if cum >= total * share:
            return level
    return 0


def extract_signature(data):
    """PNG bytes of the strokes alone, or None when nothing dark enough is
    found (a blank sheet, a picture of the ceiling)."""
    if not data or len(data) > MAX_BYTES:
        return None
    try:
        img = Image.open(io.BytesIO(data))
        if img.format == "JPEG":
            img.draft("RGB", (WORK_WIDTH * 2, WORK_WIDTH * 2))   # decode big photos at a smaller size
        img = ImageOps.exif_transpose(img)
        grey = _to_grey(img)
    except Exception:
        return None
    if grey.width > WORK_WIDTH:
        grey = grey.resize((WORK_WIDTH, max(1, int(grey.height * WORK_WIDTH / grey.width))), Image.LANCZOS)
    if grey.width < 40 or grey.height < 20:
        return None

    # How much darker each pixel is than the paper around it: 0 on the
    # sheet, high on the strokes -- whatever the lighting.
    paper, off_sheet = _paper(grey)
    dark = ImageChops.subtract(paper, grey)
    dark.paste(0, mask=off_sheet)
    hist = dark.histogram()
    total = sum(hist)
    strong = _level(hist, total, 0.002)     # the darkest strokes
    floor = _level(hist, total, 0.5)        # the paper's grain
    if strong - floor < 18:                 # nothing stands out: blank sheet, wall
        return None
    threshold = floor + (strong - floor) * 0.3
    fade = max((strong - threshold) * 0.5, 1)

    def alpha(v):
        if v <= threshold:
            return 0
        return min(255, int((v - threshold) * 255 / fade))

    mask = dark.point(alpha, "L")
    # Specks of grain and dust are a pixel or two; a stroke is wider.
    mask = mask.filter(ImageFilter.MedianFilter(3))
    inked = sum(mask.histogram()[128:])
    # Ink is part of a sheet, not most of it: past 40% it is a photo of
    # something else.
    if inked == 0 or inked > total * 0.4:
        return None
    # Crop to the strokes, leaving out faint stray dots at the edges.
    bbox = mask.point(lambda v: 255 if v >= 96 else 0).getbbox()
    if not bbox:
        return None
    pad = 6
    bbox = (max(0, bbox[0] - pad), max(0, bbox[1] - pad),
            min(mask.width, bbox[2] + pad), min(mask.height, bbox[3] + pad))
    mask = mask.crop(bbox)
    if mask.width > MAX_WIDTH:
        mask = mask.resize((MAX_WIDTH, max(1, int(mask.height * MAX_WIDTH / mask.width))), Image.LANCZOS)
    out = Image.new("RGBA", mask.size, INK + (0,))
    out.putalpha(mask)
    buf = io.BytesIO()
    out.save(buf, "PNG", optimize=True)
    return buf.getvalue()
