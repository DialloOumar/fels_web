"""Ma signature — the stamp and the strokes each person signs with.

Same approach as zone_web: a round company stamp drawn as SVG (the company's
name on the upper arc, a phone on the lower one, a word in the middle), and
the person's signature, photographed on white paper and reduced to ink by
signatures.py, laid over it. Both inks are the person's choice, and where the
strokes sit is set with sliders and previewed live.

The stamp is printed on the documents the person issues: a receipt carries
the stamp of whoever recorded the payment, a statement the stamp of whoever
prints it.
"""
import base64
import io
import unicodedata

from flask import Blueprint, abort, render_template, request, url_for
from flask_login import current_user, login_required

from app import app, get_setting, log_action
from models import User, db, utcnow

signature_bp = Blueprint("signature", __name__)

STAMP_COMPANY_FALLBACK = "FELS"
STAMP_INK = "#2b3f9e"
# A short row of inks to pick from; the colour picker takes any other.
STAMP_INKS = [("#2B3F9E", "Bleu"), ("#1F2D6B", "Bleu foncé"), ("#5B2A86", "Violet"),
              ("#B71C1C", "Rouge"), ("#1B5E20", "Vert"), ("#222222", "Noir")]

SIG_PEN = 1.1            # extra pen width on each side of the strokes, in stamp units
SIG_SHIFT_MAX = 80       # stamp units either way
SIG_SCALE_MIN, SIG_SCALE_MAX = 40, 230   # percent


def stamp_company():
    name = (get_setting("company_name", "") or "").strip()
    return (name.upper() if name else STAMP_COMPANY_FALLBACK)[:40]


def clamp_color(value, default=STAMP_INK):
    """#rrggbb, or the default."""
    v = (value or "").strip().lower()
    if len(v) == 7 and v[0] == "#" and all(ch in "0123456789abcdef" for ch in v[1:]):
        return v
    return default


def clamp_placement(dx, dy, scale):
    """The sliders' values, kept within the stamp."""
    def _int(v, default):
        try:
            return int(v)
        except (TypeError, ValueError):
            return default
    dx = max(-SIG_SHIFT_MAX, min(SIG_SHIFT_MAX, _int(dx, 0)))
    dy = max(-SIG_SHIFT_MAX, min(SIG_SHIFT_MAX, _int(dy, 0)))
    scale = max(SIG_SCALE_MIN, min(SIG_SCALE_MAX, _int(scale, 100)))
    return dx, dy, scale


def _thicken(img, radius_px):
    """The strokes with a bolder pen: a photographed ballpoint line, shrunk
    onto the stamp, comes out thinner than a pen pressed on paper. The radius
    is in the picture's own pixels, so the weight on the stamp is the same at
    every size."""
    from PIL import ImageFilter
    size = 2 * int(round(radius_px)) + 1
    if size > 1:
        alpha = img.getchannel("A").filter(ImageFilter.MaxFilter(size))
        img = img.copy()
        img.putalpha(alpha)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def stamp_signature_box(user, dx=None, dy=None, scale=None):
    """Where the strokes sit on the stamp: centred in the lower half of the
    inner circle, under the word, their aspect kept; then shifted and sized
    as set with the sliders. The canvas has a margin all round, so at full
    size the strokes run past the circle the way a real signature does."""
    from PIL import Image
    if not user.signature_png:
        return None
    try:
        img = Image.open(io.BytesIO(user.signature_png)).convert("RGBA")
    except Exception:
        return None
    w, h = img.size
    dx, dy, scale = clamp_placement(user.sig_dx if dx is None else dx,
                                    user.sig_dy if dy is None else dy,
                                    user.sig_scale if scale is None else scale)
    box_w = 200.0
    box_h = box_w * h / w if w else 68
    if box_h > 92:
        box_h, box_w = 92.0, 92.0 * w / h
    box_w, box_h = box_w * scale / 100.0, box_h * scale / 100.0
    return dict(b64=base64.b64encode(_thicken(img, SIG_PEN * w / box_w)).decode("ascii"),
                w=round(box_w, 1), h=round(box_h, 1),
                x=round(150 - box_w / 2 + dx, 1), y=round(204 - box_h / 2 + dy, 1))


# Advance widths of Arial Bold, per 1000 em, for the word in the middle; a
# letter not listed counts as a wide one.
_LABEL_WIDTHS = {
    "A": 722, "B": 722, "C": 722, "D": 722, "E": 667, "F": 611, "G": 778, "H": 722,
    "I": 278, "J": 556, "K": 722, "L": 611, "M": 833, "N": 722, "O": 778, "P": 667,
    "Q": 778, "R": 722, "S": 667, "T": 611, "U": 722, "V": 667, "W": 944, "X": 667,
    "Y": 667, "Z": 611, "a": 556, "b": 611, "c": 556, "d": 611, "e": 556, "f": 333,
    "g": 611, "h": 611, "i": 278, "j": 278, "k": 556, "l": 278, "m": 889, "n": 611,
    "o": 611, "p": 611, "q": 611, "r": 389, "s": 556, "t": 333, "u": 611, "v": 556,
    "w": 778, "x": 556, "y": 556, "z": 500, " ": 278, "-": 333, "&": 722, ".": 278,
    "/": 278, "'": 238,
}
LABEL_SIZE_MAX, LABEL_SIZE_MIN = 34, 14
STAMP_INNER_R = 98      # the inner circle, as drawn in stamp.svg


def stamp_label_fit(label, y_offset):
    """(font size, text length) keeping the word inside the inner circle:
    the largest size up to the usual one at which its estimated width fits
    the circle's chord at the word's height. text_length holds the word to
    that width when it had to shrink; None when it fit at full size."""
    if not label:
        return LABEL_SIZE_MAX, None

    def width(ch):
        base = unicodedata.normalize("NFD", ch)[0]
        return _LABEL_WIDTHS.get(base, 722 if ch.isupper() else 611)

    em = sum(width(ch) for ch in label) / 1000.0
    chord = 2 * (STAMP_INNER_R ** 2 - y_offset ** 2) ** 0.5
    available = chord - 16
    if em * LABEL_SIZE_MAX <= available:
        return LABEL_SIZE_MAX, None
    size = max(LABEL_SIZE_MIN, int(available / em))
    return size, round(min(available, em * size), 1)


def user_has_stamp(user):
    """Whether there is anything to print for this person."""
    return bool(user and (user.stamp_label or user.signature_png))


@app.context_processor
def _inject_stamp():
    return {"user_has_stamp": user_has_stamp}


@signature_bp.route("/tampon/<int:user_id>.svg")
@login_required
def stamp_svg(user_id):
    """One person's stamp with their strokes over it, as an SVG image."""
    user = db.session.get(User, user_id) or abort(404)
    label = (user.stamp_label or "").strip()
    phone = (user.stamp_phone or "").strip()
    color = clamp_color(user.stamp_color)
    sig_color = clamp_color(user.sig_color, color)
    box = stamp_signature_box(user)
    # One's own stamp can be previewed with what is being typed and slid,
    # before it is saved: the page passes it along, nothing is written.
    if (user.id == current_user.id or current_user.is_super_admin) and any(
            k in request.args for k in ("label", "phone", "dx", "dy", "scale", "color", "sig_color")):
        label = (request.args.get("label") or "").strip()[:40]
        phone = (request.args.get("phone") or "").strip()[:30]
        color = clamp_color(request.args.get("color"))
        sig_color = clamp_color(request.args.get("sig_color"), color)
        box = stamp_signature_box(user, request.args.get("dx"), request.args.get("dy"), request.args.get("scale"))
    label_size, label_length = stamp_label_fit(label, 18 if box else 0)
    svg = render_template("stamp.svg", company=stamp_company(), phone=phone, label=label,
                          label_size=label_size, label_length=label_length,
                          signature=box, color=color, sig_color=sig_color)
    resp = app.response_class(svg, mimetype="image/svg+xml")
    resp.headers["Cache-Control"] = "private, max-age=60"
    return resp


def save_signature_form(user):
    """Copy the stamp-and-signature form onto `user` and commit. Returns
    (error, success), message keys for the page. Shared by Ma signature and
    the super admin's page for someone else's signature."""
    from signatures import extract_signature
    error = None
    user.stamp_label = (request.form.get("stamp_label") or "").strip()[:40] or None
    user.stamp_phone = (request.form.get("stamp_phone") or "").strip()[:30] or None
    user.sig_dx, user.sig_dy, user.sig_scale = clamp_placement(
        request.form.get("sig_dx"), request.form.get("sig_dy"), request.form.get("sig_scale"))
    user.stamp_color = clamp_color(request.form.get("stamp_color"))
    user.sig_color = clamp_color(request.form.get("sig_color"), user.stamp_color)
    if request.form.get("remove_signature"):
        user.signature_png = None
        user.signature_at = None
    f = request.files.get("signature")
    if f and f.filename:
        png = extract_signature(f.read())
        if not png:
            error = "sig.err.unreadable"
        else:
            user.signature_png = png
            user.signature_at = utcnow()
            user.sig_dx, user.sig_dy, user.sig_scale = 0, 0, 100   # a new signature starts centred
    if error:
        db.session.rollback()
        return error, None
    detail = ("signature retirée" if request.form.get("remove_signature")
              else "nouvelle signature" if f and f.filename else None)
    if user.id != current_user.id:
        detail = "%s · par %s" % (detail or "tampon modifié", current_user.username)
    log_action("UPDATE", "signature", user.id, detail)
    db.session.commit()
    return None, "sig.saved"


def render_signature_page(user, error=None, success=None, back_url=None):
    """The stamp-and-signature page for `user`. With back_url it is someone
    else's, opened by the super admin from the Users page."""
    return render_template("my_signature.html", who=user, error=error, success=success,
                           back_url=back_url,
                           inks=STAMP_INKS, ink=clamp_color(user.stamp_color),
                           sig_ink=clamp_color(user.sig_color, clamp_color(user.stamp_color)),
                           has_signature=bool(user.signature_png),
                           stamp_version=int(user.signature_at.timestamp()) if user.signature_at else 0)


@signature_bp.route("/ma-signature", methods=["GET", "POST"])
@login_required
def mine():
    """Where each person sets up their own stamp and signature."""
    error = success = None
    if request.method == "POST":
        error, success = save_signature_form(current_user)
    return render_signature_page(current_user, error, success)
