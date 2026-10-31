"""Fels Web — Flask application entry point.

Holds what every page shares: configuration, the login, the CSRF guard,
translations, the theme, money and date formatting, the settings, the error
journal, and the bootstrap CLI commands. Each business area (suppliers,
customers, products, settings...) lives in its own blueprint under
blueprints/, registered at the bottom of this file.
"""
import logging
import mimetypes
import os
import re
import secrets
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

import click
from dotenv import load_dotenv
from flask import (Flask, abort, g, jsonify, redirect, render_template,
                   request, session, url_for)
from flask_login import (LoginManager, current_user, login_required,
                         login_user, logout_user)
from flask_migrate import Migrate
from markupsafe import Markup
from werkzeug.middleware.proxy_fix import ProxyFix

from languages import TRANSLATIONS
from models import (CURRENCIES, AppSetting, AuditLog, ErrorEvent, Port, User,
                    convert, db, utcnow)

load_dotenv()

# The slim Python image has no font types: serve the self-hosted fonts as fonts.
mimetypes.add_type("font/woff2", ".woff2")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")
log = logging.getLogger("fels_web")

APP_VERSION = "0.1.0"


def _env_flag(name, default):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


# ── App ──────────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-fallback-change-me")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", "sqlite:///fels.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
# The session cookie: unreadable from scripts, never sent by another site's
# form, and over HTTPS only once Caddy is in front.
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = _env_flag("SESSION_COOKIE_SECURE", "1")
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=14)

# Caddy sits in front: trust its X-Forwarded-For / -Proto, one hop.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

db.init_app(app)
migrate = Migrate(app, db, compare_type=True)

login_manager = LoginManager(app)
login_manager.login_view = "login"
# The login screen already says why you are there; Flask-Login's English
# "Please log in" would otherwise survive the redirect onto the first page.
login_manager.login_message = None


@login_manager.user_loader
def _load_user(user_id):
    u = db.session.get(User, int(user_id))
    return u if u and u.is_active else None


def _static_version():
    """Cache-busting stamp for /static URLs: the newest mtime under static/,
    computed once at start-up, so a deploy with changed CSS or JS is picked
    up by every browser without anyone remembering to bump a number."""
    newest = 0
    for root, _dirs, files in os.walk(app.static_folder):
        for name in files:
            try:
                newest = max(newest, os.stat(os.path.join(root, name)).st_mtime)
            except OSError:
                pass
    return str(int(newest)) if newest else APP_VERSION


STATIC_VERSION = _static_version()


# ── CSRF ─────────────────────────────────────────────────────────────────────
# One token per session, carried by every form as a hidden field (and by the
# modal layer's fetch, which posts the form as it is). A POST without it is
# refused before any view runs.

def csrf_token():
    tok = session.get("_csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        session["_csrf"] = tok
    return tok


def csrf_field():
    return Markup('<input type="hidden" name="csrf_token" value="%s">' % csrf_token())


@app.before_request
def _enforce_roles():
    """What the user's role allows, checked once for every request."""
    from access import enforce
    return enforce()


@app.before_request
def _csrf_protect():
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
        expected = session.get("_csrf") or ""
        if not expected or not secrets.compare_digest(sent, expected):
            abort(400)


# ── i18n ─────────────────────────────────────────────────────────────────────

LANGS = ("fr", "en", "de")
# Each language under its own name, as the language menu lists them.
LANG_NAMES = (("fr", "Français"), ("en", "English"), ("de", "Deutsch"))


class _Translations(dict):
    """A language's dictionary that never raises: a key missing from English
    or German falls back to French, and a key missing everywhere shows as
    itself, so a forgotten translation is visible on the page instead of
    blank."""

    def __missing__(self, key):
        return TRANSLATIONS["fr"].get(key, key)


_T = {lang: _Translations(TRANSLATIONS[lang]) for lang in TRANSLATIONS}


def current_lang():
    if current_user.is_authenticated and current_user.lang in LANGS:
        return current_user.lang
    if session.get("lang") in LANGS:
        return session["lang"]
    return os.environ.get("APP_DEFAULT_LANG", "fr")


def get_t():
    return _T.get(current_lang(), _T["fr"])


@app.route("/set-language/<lang>")
def set_language(lang):
    if lang in LANGS:
        session["lang"] = lang
        if current_user.is_authenticated:
            current_user.lang = lang
            db.session.commit()
    return redirect(_safe_back(request.referrer) or url_for("dashboard"))


THEMES = ("light", "dark")


@app.route("/set-theme/<theme>")
def set_theme(theme):
    """Light or dark, remembered for a year in a cookie: it has to apply to
    the login page too, before anyone is known."""
    if theme not in THEMES:
        abort(404)
    resp = redirect(_safe_back(request.referrer) or url_for("dashboard"))
    resp.set_cookie("theme", theme, max_age=60 * 60 * 24 * 365, samesite="Lax",
                    secure=app.config["SESSION_COOKIE_SECURE"])
    return resp


def _safe_back(url):
    """Only a page of this site: never bounce someone to an address another
    site slipped into the referrer or a ?next=."""
    if not url:
        return None
    if url.startswith("/") and not url.startswith("//"):
        return url
    host = request.host_url
    return url if url.startswith(host) else None


# ── Settings ─────────────────────────────────────────────────────────────────
# Edited on the Paramètres page. `group` orders them into blocks there.

SETTINGS_SCHEMA = {
    # The rate every conversion starts from: GNF for 1 USD. Copied onto each
    # payment and cost when it is entered, so changing it never rewrites the
    # past. Empty until the client sets it: no conversion is better than a
    # wrong one.
    "usd_gnf_rate":     {"group": "currency", "type": "rate",   "default": ""},
    # The currency the dashboard totals are shown in first.
    "display_currency": {"group": "currency", "type": "select", "default": "USD", "options": CURRENCIES},
    # Printed at the top of receipts and statements.
    "company_name":     {"group": "company",  "type": "text",   "default": ""},
    "company_phone":    {"group": "company",  "type": "text",   "default": ""},
    "company_email":    {"group": "company",  "type": "text",   "default": ""},
    "company_address":  {"group": "company",  "type": "text",   "default": ""},
    "receipt_footer":   {"group": "company",  "type": "textarea", "default": ""},
    # The bell: after how many days a purchase at one stage, and a rate not
    # re-entered, are worth a word.
    "alert_stuck_days": {"group": "alerts",   "type": "int",    "default": "30", "min": 1, "max": 365},
    "alert_rate_days":  {"group": "alerts",   "type": "int",    "default": "7",  "min": 1, "max": 365},
}


def get_setting(key, default=None):
    """A setting's value, read once per request."""
    cache = g.setdefault("_settings", {})
    if key not in cache:
        row = db.session.get(AppSetting, key)
        cache[key] = row.value if row is not None else SETTINGS_SCHEMA.get(key, {}).get("default")
    val = cache[key]
    return default if val in (None, "") and default is not None else val


def set_setting(key, value):
    row = db.session.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key, value=value or "")
        db.session.add(row)
    else:
        row.value = value or ""
        # Saved means confirmed, even unchanged: re-entering today's rate as
        # it was is what tells the bell the rate is current.
        row.updated_at = utcnow()
    g.setdefault("_settings", {})[key] = value or ""


def current_rate():
    """Today's GNF-per-USD rate from the settings, or None when unset."""
    raw = get_setting("usd_gnf_rate", "")
    try:
        r = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    return r if r > 0 else None


def other_currency(cur):
    return "GNF" if cur == "USD" else "USD"


# ── Money, read and written ──────────────────────────────────────────────────
#
# Figures run to nine digits in GNF, and a wall of them is unreadable, so every
# amount on screen is grouped in threes -- and so is every amount being typed
# (main.js). The separator is a no-break space, never a comma: French reads a
# comma as the decimal mark. Cents follow a comma, only when there are any.

GROUP = " "


@app.template_filter("num")
def _num(value):
    if value is None or value == "":
        return "—"
    if isinstance(value, Decimal):
        cents = value.quantize(Decimal("0.01"))
        if cents != cents.to_integral_value():
            whole, frac = "{:.2f}".format(abs(cents)).split(".")
            sign = "-" if cents < 0 else ""
            return sign + "{:,}".format(int(whole)).replace(",", GROUP) + "," + frac
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return value
    return "{:,}".format(n).replace(",", GROUP)


@app.template_filter("money")
def _money(value, currency):
    """1 250 000 GNF, 10 000 USD, 9 950,50 USD -- or a dash when unknown."""
    if value is None:
        return "—"
    return "%s %s" % (_num(Decimal(value)), currency)


@app.template_filter("money_field")
def _money_field(value):
    """An amount as a form field holds it: digits, and a comma before the
    cents only when there are cents. The page groups it as it is shown."""
    if value is None or value == "":
        return ""
    try:
        d = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return value
    if d == d.to_integral_value():
        return str(int(d))
    return "{:.2f}".format(d).replace(".", ",")


@app.template_filter("fdate")
def _fdate(value):
    """A date as it is written here: 21/10/2026."""
    if not value:
        return "—"
    if isinstance(value, (date, datetime)):
        return value.strftime("%d/%m/%Y")
    return value


def parse_money(raw):
    """A typed amount back to a Decimal with two places, or None.

    "1 250 000", "1250000", "9 950,50" and "9950.50" all read. A comma or a
    point is the decimal mark only as the last mark with one or two digits
    after it; any other is a grouping mark, as in "1.500.000"."""
    s = (raw or "").strip()
    for ch in (" ", " ", " ", " ", "'"):
        s = s.replace(ch, "")
    if not s:
        return None
    m = re.search(r"[.,](\d{1,2})$", s)
    whole, frac = (s[:m.start()], m.group(1)) if m else (s, "")
    whole = whole.replace(",", "").replace(".", "") or "0"
    if not whole.lstrip("-").isdigit():
        return None
    try:
        return Decimal(whole + ("." + frac if frac else "")).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def parse_rate(raw):
    """A rate (GNF per USD): a positive number, decimals allowed."""
    s = (raw or "").strip().replace(" ", "").replace(" ", "").replace(",", ".")
    if not s:
        return None
    try:
        r = Decimal(s)
    except InvalidOperation:
        return None
    return r if r > 0 else None


def parse_date(raw):
    try:
        return date.fromisoformat((raw or "").strip())
    except ValueError:
        return None


def converted(amount, currency, rate=None):
    """`amount` shown in the other currency, at `rate` or today's -- the grey
    figure beside an amount. None when there is no rate to convert with."""
    if amount is None:
        return None
    to = other_currency(currency)
    value = convert(amount, currency, to, rate if rate is not None else current_rate())
    return None if value is None else (value, to)


# ── Templates ────────────────────────────────────────────────────────────────


def _page_args():
    """Every query argument but the page number, so a pager keeps the
    filters that made the list."""
    args = request.args.to_dict(flat=False)
    args.pop("page", None)
    return args


@app.context_processor
def _inject_globals():
    return {
        "t": get_t(),
        "lang": current_lang(),
        "lang_names": LANG_NAMES,
        "v": STATIC_VERSION,
        "app_version": APP_VERSION,
        "csrf_field": csrf_field,
        "page_args": _page_args,
        "get_setting": get_setting,
        "current_rate": current_rate,
        "converted": converted,
        "currencies": CURRENCIES,
        "today": date.today(),
        "is_super_admin": current_user.is_authenticated and current_user.is_super_admin,
        # can('sales', 'manage'): what the role allows, to show or hide links.
        "can": _can,
        "theme": request.cookies.get("theme") if request.cookies.get("theme") in THEMES else "",
    }


def _can(area, level="view"):
    from access import can
    return can(area, level)


def is_modal_request():
    """True when the request comes from the modal layer (fetch + header): the
    form is served as a bare partial and a success answers with JSON."""
    return request.headers.get("X-Requested-With") == "fetch"


def modal_ok():
    """Success answer to a modal form: the page reloads to show the flash
    and the refreshed list."""
    return jsonify(ok=True)


def form_response(full_template, partial_template, **ctx):
    """Render a form as a whole page, or as the bare partial when the modal
    asked for it."""
    return render_template(partial_template if is_modal_request() else full_template, **ctx)


PER_PAGE = 50


def paginate(query):
    page = request.args.get("page", 1, type=int)
    return db.paginate(query, page=max(page, 1), per_page=PER_PAGE, error_out=False)


class ListPage:
    """One page of a list already filtered in Python -- for filters on a
    computed value, such as a vehicle's status, which SQL cannot see. Same
    attributes as Flask-SQLAlchemy's pagination, so _pager.html serves both."""

    def __init__(self, rows, per_page=PER_PAGE):
        self.total = len(rows)
        self.per_page = per_page
        self.pages = max(1, -(-self.total // per_page))
        self.page = min(max(request.args.get("page", 1, type=int), 1), self.pages)
        start = (self.page - 1) * per_page
        self.items = rows[start:start + per_page]
        self.first = start + 1 if self.total else 0
        self.last = start + len(self.items)
        self.has_prev, self.has_next = self.page > 1, self.page < self.pages
        self.prev_num, self.next_num = self.page - 1, self.page + 1


def rate_field_value(rate):
    """A rate as a form field shows it: 8650.5, no trailing zeros."""
    if rate is None:
        return ""
    return format(Decimal(rate).normalize(), "f")


app.jinja_env.filters["rate_field"] = rate_field_value


# ── Audit log ────────────────────────────────────────────────────────────────


def log_action(action, resource_type, resource_id=None, detail=None, username=None):
    """Note a change in the audit log. The row joins the caller's
    transaction: it is saved by the caller's commit, and lost with it on a
    rollback, so the log never claims a change that did not happen. Pass
    `username` when nobody is signed in (a failed sign-in). Never raises --
    the log must not break the change it describes."""
    try:
        who = current_user if current_user.is_authenticated else None
        if who is not None:
            role = "super_admin" if who.is_super_admin else (who.role.name if who.role else None)
        db.session.add(AuditLog(
            user_id=who.id if who else None,
            username=((who.username if who else username) or "?")[:80],
            actor_role=role[:80] if who and role else None,
            action=action, resource_type=resource_type, resource_id=resource_id,
            detail=(detail or "")[:255] or None,
            ip_address=(request.remote_addr or "")[:45] or None))
    except Exception:
        log.exception("audit log failed: %s %s", action, resource_type)


def fr(key):
    """A French label for an audit detail. The detail is written once and
    read in either language, so it is kept in the client's own."""
    return TRANSLATIONS["fr"].get(key, key)


money_text = _money


# ── Auth ─────────────────────────────────────────────────────────────────────


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        from access import home_url
        return redirect(home_url())
    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()
        password = request.form.get("password") or ""
        user = User.query.filter_by(username=username).first()
        if user and user.is_active and user.check_password(password):
            session.clear()                      # a fresh session, a fresh CSRF token
            session.permanent = True
            login_user(user)
            user.last_login_at = utcnow()
            log_action("LOGIN", "auth")
            db.session.commit()
            from access import home_url
            return redirect(_safe_back(request.args.get("next")) or home_url())
        log.warning("failed login from %s", request.remote_addr)
        log_action("LOGIN_FAILED", "auth", username=username or "?",
                   detail="compte désactivé" if user and not user.is_active else None)
        db.session.commit()
        error = get_t()["auth.invalid_credentials"]
    return render_template("login.html", error=error)


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    log_action("LOGOUT", "auth")
    db.session.commit()
    logout_user()
    session.clear()
    return redirect(url_for("login"))


@app.route("/password", methods=["GET", "POST"])
@login_required
def change_password():
    error = success = None
    if request.method == "POST":
        current_pw = request.form.get("current_password", "")
        new_pw = request.form.get("new_password", "")
        confirm_pw = request.form.get("confirm_password", "")
        if not current_user.check_password(current_pw):
            error = "pw.error.wrong_current"
        elif len(new_pw) < 8:
            error = "pw.error.too_short"
        elif new_pw != confirm_pw:
            error = "pw.error.no_match"
        else:
            current_user.set_password(new_pw)
            log_action("UPDATE", "user", current_user.id, "mot de passe changé")
            db.session.commit()
            success = "pw.success"
    return render_template("change_password.html", error=error, success=success)


@app.route("/healthz")
def healthz():
    """Liveness probe."""
    return jsonify(status="ok", version=APP_VERSION), 200


# ── Dashboard ────────────────────────────────────────────────────────────────


@app.route("/")
@login_required
def dashboard():
    from access import can, home_url
    if not can("dashboard"):
        return redirect(home_url())
    from models import Customer, Product, Supplier
    counts = {
        "suppliers": Supplier.query.filter_by(is_active=True).count(),
        "products":  Product.query.filter_by(is_active=True).count(),
        "customers": Customer.query.filter_by(is_active=True).count(),
    }
    rate = current_rate()
    # What to set up first, in the order it is needed. Ticked as it is done.
    steps = [
        {"key": "rate",      "icon": "swap_horiz", "done": rate is not None,
         "url": url_for("settings.index")},
        {"key": "company",   "icon": "settings",   "done": bool(get_setting("company_name", "")),
         "url": url_for("settings.index")},
        {"key": "suppliers", "icon": "store",      "done": counts["suppliers"] > 0,
         "url": url_for("suppliers.index")},
        {"key": "products",  "icon": "category",   "done": counts["products"] > 0,
         "url": url_for("products.index")},
        {"key": "customers", "icon": "handshake",  "done": counts["customers"] > 0,
         "url": url_for("customers.index")},
    ]
    from blueprints.dashboard import compute
    return render_template("dashboard.html", counts=counts, rate=rate, steps=steps, d=compute())


# ── Errors ───────────────────────────────────────────────────────────────────


@app.errorhandler(400)
def _bad_request(_):
    return render_template("error.html", code=400, message=get_t()["error.bad_request"]), 400


@app.errorhandler(403)
def _forbidden(_):
    return render_template("error.html", code=403, message=get_t()["error.forbidden"]), 403


@app.errorhandler(404)
def _not_found(_):
    return render_template("error.html", code=404, message=get_t()["error.not_found"]), 404


@app.errorhandler(Exception)
def _server_error(exc):
    """Anything the code did not expect. The half-done transaction is rolled
    back; the traceback is kept in the error journal under a reference the
    user is shown, so "I got ERR-7F3A2C" is enough to find it. Inside a
    dialog the same message comes back as a fragment."""
    from werkzeug.exceptions import HTTPException
    if isinstance(exc, HTTPException):
        return exc
    import traceback as tb
    ref = "ERR-" + secrets.token_hex(3).upper()
    text = "".join(tb.format_exception(type(exc), exc, exc.__traceback__))
    log.exception("%s %s %s", ref, request.method, request.path)
    try:
        db.session.rollback()
        who = current_user if current_user.is_authenticated else None
        db.session.add(ErrorEvent(
            ref=ref, method=request.method, path=request.full_path.rstrip("?")[:300],
            user_id=who.id if who else None, username=who.username if who else None,
            message=("%s: %s" % (type(exc).__name__, exc))[:300], traceback=text[-20000:],
            form_keys=",".join(sorted(request.form.keys()))[:300] if request.form else None))
        ErrorEvent.query.filter(ErrorEvent.created_at < utcnow() - timedelta(days=90)).delete()
        db.session.commit()
    except Exception:          # the journal must never be a second error
        db.session.rollback()
        log.exception("could not record %s", ref)
    t = get_t()
    if is_modal_request():
        return render_template("_error_fragment.html", ref=ref), 500
    return render_template("error.html", code=500, ref=ref, message=t["error.server"]), 500


# ── CLI ──────────────────────────────────────────────────────────────────────

DEFAULT_PORTS = ("Boffa", "Boké")


def _seed_super_admin_from_env():
    """Create the super admin from ADMIN_USERNAME / ADMIN_PASSWORD if none
    exists yet. A no-op on every later boot."""
    if User.query.filter_by(is_super_admin=True).first():
        return False
    username = os.environ.get("ADMIN_USERNAME", "admin").strip().lower()
    password = os.environ.get("ADMIN_PASSWORD", "").strip()
    if not password:
        raise RuntimeError("ADMIN_PASSWORD is required to bootstrap the super admin.")
    if User.query.filter_by(username=username).first():
        raise RuntimeError("A user named '%s' exists but is not the super admin." % username)
    email = (os.environ.get("ADMIN_EMAIL") or "").strip().lower() or None
    u = User(username=username, email=email, is_super_admin=True,
             full_name=os.environ.get("ADMIN_FULL_NAME", "Administrateur"),
             lang=os.environ.get("APP_DEFAULT_LANG", "fr"))
    u.set_password(password)
    db.session.add(u)
    db.session.commit()
    return True


@app.cli.command("seed")
def seed_cmd():
    """Bootstrap: settings, ports, the super admin. Idempotent, run on every
    boot by entrypoint.sh."""
    added = 0
    for key, spec in SETTINGS_SCHEMA.items():
        if db.session.get(AppSetting, key) is None:
            db.session.add(AppSetting(key=key, value=spec["default"]))
            added += 1
    db.session.commit()
    click.echo("  settings: %d added" % added)

    if Port.query.count() == 0:
        for i, name in enumerate(DEFAULT_PORTS):
            db.session.add(Port(name=name, sort_order=i))
        db.session.commit()
        click.echo("  ports: %s" % ", ".join(DEFAULT_PORTS))
    else:
        click.echo("  ports: already set, skipped")

    created = _seed_super_admin_from_env()
    click.echo("  super admin: %s" % ("created from ADMIN_USERNAME" if created else "already exists, skipped"))

    # Starter roles, on a fresh install only: once roles exist they are the
    # administrator's, and a deleted one is not brought back.
    from access import SEED_ROLES
    from models import Role
    if Role.query.count() == 0:
        for name, description, perms in SEED_ROLES:
            db.session.add(Role(name=name, description=description, permissions=perms))
        db.session.commit()
        click.echo("  roles: %s" % ", ".join(r[0] for r in SEED_ROLES))
    else:
        click.echo("  roles: already set, skipped")
    click.echo("Seed complete.")


@app.cli.command("seed-super-admin")
@click.option("--username", required=True)
@click.option("--password", required=True)
@click.option("--full-name", default="Administrateur")
@click.option("--email", default=None)
def seed_super_admin_cmd(username, password, full_name, email):
    """Create the super admin from flags, for an install that keeps the
    password out of .env."""
    if User.query.filter_by(is_super_admin=True).first():
        click.echo("A super admin already exists. Use reset-super-admin-password.", err=True)
        raise SystemExit(1)
    username = username.strip().lower()
    if User.query.filter_by(username=username).first():
        click.echo("A user named '%s' already exists." % username, err=True)
        raise SystemExit(1)
    u = User(username=username, full_name=full_name, is_super_admin=True,
             email=(email.strip().lower() if email else None),
             lang=os.environ.get("APP_DEFAULT_LANG", "fr"))
    u.set_password(password)
    db.session.add(u)
    db.session.commit()
    click.echo("Super admin created: %s" % username)


@app.cli.command("reset-super-admin-password")
@click.option("--password", required=True)
def reset_super_admin_password_cmd(password):
    """Recovery: set a new password on the super admin."""
    u = User.query.filter_by(is_super_admin=True).first()
    if not u:
        click.echo("No super admin found. Run `flask seed` first.", err=True)
        raise SystemExit(1)
    u.set_password(password)
    db.session.commit()
    click.echo("Password reset for %s." % u.username)


# ── Blueprints ───────────────────────────────────────────────────────────────
# Imported after the helpers above exist, so blueprint modules can
# `from app import ...` without a circular import.
from blueprints.settings import settings_bp  # noqa: E402
from blueprints.suppliers import suppliers_bp  # noqa: E402
from blueprints.customers import customers_bp  # noqa: E402
from blueprints.products import products_bp  # noqa: E402
from blueprints.purchases import purchases_bp  # noqa: E402
from blueprints.payments import payments_bp  # noqa: E402
from blueprints.stock import stock_bp  # noqa: E402
from blueprints.sales import sales_bp  # noqa: E402
from blueprints.importer import importer_bp  # noqa: E402
from blueprints.signature import signature_bp  # noqa: E402
from blueprints.notifications import notifications_bp  # noqa: E402
from blueprints.users import users_bp  # noqa: E402
from blueprints.journal import journal_bp  # noqa: E402

app.register_blueprint(settings_bp)
app.register_blueprint(suppliers_bp)
app.register_blueprint(customers_bp)
app.register_blueprint(products_bp)
app.register_blueprint(purchases_bp)
app.register_blueprint(payments_bp)
app.register_blueprint(stock_bp)
app.register_blueprint(sales_bp)
app.register_blueprint(importer_bp)
app.register_blueprint(signature_bp)
app.register_blueprint(notifications_bp)
app.register_blueprint(users_bp)
app.register_blueprint(journal_bp)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
