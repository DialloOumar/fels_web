"""A signature drawn on white paper, photographed, turned into ink on glass.

The person signs on a blank sheet and uploads the picture. What comes back
is a small transparent PNG holding only the strokes: the paper, the shadows
and the lighting are gone, so the signature can be laid over the company
stamp and printed on a receipt as if it had been signed there.

How: the picture is turned to greys and its contrast stretched; the paper is
found as the bright side of the histogram; anything clearly darker than the
paper is ink, and how much darker sets how opaque the pixel is, so thin
strokes keep soft edges. The result is cropped to the strokes and scaled to
a printable width. (Same method as zone_web.)
"""
import io

from PIL import Image, ImageOps

INK = (43, 63, 158)      # the stamp's blue, #2B3F9E
MAX_WIDTH = 900
MAX_BYTES = 8 * 1024 * 1024


def extract_signature(data):
    """PNG bytes of the strokes alone, or None when nothing dark enough is
    found (a blank sheet, a picture of the ceiling)."""
    if not data or len(data) > MAX_BYTES:
        return None
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
    except Exception:
        return None
    grey = ImageOps.autocontrast(img.convert("L"), cutoff=1)
    if grey.width > 1600:
        grey = grey.resize((1600, int(grey.height * 1600 / grey.width)), Image.LANCZOS)

    # The paper is the bright mass of the histogram; ink is what sits well
    # below it. Halfway between the paper and the darkest strokes is the
    # line, and the band just under it fades in rather than cutting hard.
    hist = grey.histogram()
    total = sum(hist)
    cum, paper = 0, 255
    for level in range(255, -1, -1):
        cum += hist[level]
        if cum >= total * 0.5:
            paper = level
            break
    cum, ink_floor = 0, 0
    for level in range(256):
        cum += hist[level]
        if cum >= total * 0.001:   # a thin pen is a tenth of a percent of the sheet
            ink_floor = level
            break
    # On a photographed sheet the paper is the bright mass: after the
    # stretch, most pixels sit near white. A median far from it means the
    # picture is a gradient or a wall, not paper with strokes on it.
    if paper < 190 or paper - ink_floor < 40:
        return None
    threshold = ink_floor + (paper - ink_floor) * 0.55
    fade = max((threshold - ink_floor) * 0.5, 1)

    def alpha(v):
        if v >= threshold:
            return 0
        return min(255, int((threshold - v) * 255 / fade))

    mask = grey.point(alpha, "L")
    # Ink is a small part of a sheet. A picture where more than an eighth
    # of the pixels count as ink is a shadow or a gradient, not a signature.
    inked = sum(mask.histogram()[128:])
    if inked == 0 or inked > total * 0.12:
        return None
    bbox = mask.getbbox()
    if not bbox:
        return None
    mask = mask.crop(bbox)
    if mask.width > MAX_WIDTH:
        mask = mask.resize((MAX_WIDTH, max(1, int(mask.height * MAX_WIDTH / mask.width))), Image.LANCZOS)
    out = Image.new("RGBA", mask.size, INK + (0,))
    out.putalpha(mask)
    buf = io.BytesIO()
    out.save(buf, "PNG", optimize=True)
    return buf.getvalue()
