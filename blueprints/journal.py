"""Journaux — the activity log (who did what, and when) and the error
journal, two tabs of one page. The super admin's alone: they read over
everyone's shoulder, and the errors carry tracebacks."""
from datetime import timedelta

from flask import Blueprint, render_template, request
from flask_login import login_required

from app import get_t, paginate, parse_date
from models import AuditLog, ErrorEvent, db

journal_bp = Blueprint("journal", __name__, url_prefix="/parametres")

# What a log line can link to, while the thing still exists.
LINKS = {
    "purchase": ("purchases.detail", "pid"),
    "sale":     ("sales.detail", "sid"),
    "supplier": ("suppliers.detail", "sid"),
    "customer": ("customers.detail", "cid"),
}


def _tab_counts():
    return {"audit": AuditLog.query.count(), "errors": ErrorEvent.query.count()}


def _options(column, prefix):
    """The values the log holds in `column`, as (value, label) for a dropdown."""
    t = get_t()
    values = [v for (v,) in db.session.query(column).distinct().all()]
    return sorted(((v, t[prefix + v]) for v in values), key=lambda o: o[1].lower())


@journal_bp.route("/journal")
@login_required
def audit():
    a = request.args
    search = (a.get("q") or "").strip()
    action = a.get("action") or ""
    resource = a.get("resource") or ""
    date_from, date_to = parse_date(a.get("from")), parse_date(a.get("to"))
    q = AuditLog.query
    if search:
        like = "%" + search + "%"
        q = q.filter(db.or_(AuditLog.username.ilike(like), AuditLog.detail.ilike(like)))
    if action:
        q = q.filter(AuditLog.action == action)
    if resource:
        q = q.filter(AuditLog.resource_type == resource)
    if date_from:
        q = q.filter(AuditLog.timestamp >= date_from)
    if date_to:
        q = q.filter(AuditLog.timestamp < date_to + timedelta(days=1))
    pagination = paginate(q.order_by(AuditLog.timestamp.desc(), AuditLog.id.desc()))
    return render_template("audit.html", pagination=pagination, rows=pagination.items,
                           actions=_options(AuditLog.action, "audit.action."),
                           resources=_options(AuditLog.resource_type, "audit.resource."),
                           search=search, action=action, resource=resource,
                           date_from=a.get("from") or "", date_to=a.get("to") or "",
                           links=LINKS, tab_counts=_tab_counts())


@journal_bp.route("/erreurs")
@login_required
def errors():
    q = ErrorEvent.query
    ref = (request.args.get("q") or "").strip()
    if ref:
        q = q.filter(ErrorEvent.ref.ilike("%" + ref + "%"))
    pagination = paginate(q.order_by(ErrorEvent.created_at.desc()))
    return render_template("errors.html", pagination=pagination, rows=pagination.items, search=ref,
                           tab_counts=_tab_counts())
