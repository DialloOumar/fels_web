"""Paramètres — the rate, the display currency, the company details printed
on receipts, and the list of ports."""
from decimal import Decimal, InvalidOperation

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import login_required

from app import (SETTINGS_SCHEMA, form_response, get_setting, get_t,
                 is_modal_request, log_action, modal_ok, parse_rate, set_setting)
from models import CURRENCIES, Port, db

settings_bp = Blueprint("settings", __name__, url_prefix="/parametres")

GROUPS = ("currency", "company", "invoice", "alerts")


@settings_bp.route("/", methods=["GET", "POST"])
@login_required
def index():
    t = get_t()
    errors, form = {}, None
    if request.method == "POST":
        form = {k: (request.form.get(k) or "").strip() for k in SETTINGS_SCHEMA}
        raw_rate = form["usd_gnf_rate"]
        if raw_rate:
            rate = parse_rate(raw_rate)
            if rate is None:
                errors["usd_gnf_rate"] = t["settings.error.rate"]
            else:
                form["usd_gnf_rate"] = format(rate.normalize(), "f")
        if form["display_currency"] not in CURRENCIES:
            errors["display_currency"] = t["settings.error.currency"]
        for key, spec in SETTINGS_SCHEMA.items():
            if spec["type"] == "select" and key != "display_currency" and form[key] not in spec["options"]:
                form[key] = spec["default"]
            if spec["type"] == "decimal":
                raw = form[key].replace(",", ".") or spec["default"]
                try:
                    n = Decimal(raw)
                    ok = spec["min"] <= n <= spec["max"]
                except InvalidOperation:
                    ok = False
                if ok:
                    form[key] = format(n.normalize(), "f")
                else:
                    errors[key] = t["settings.error.decimal"] % {"min": spec["min"], "max": spec["max"]}
        for key, spec in SETTINGS_SCHEMA.items():
            if spec["type"] != "int":
                continue
            if not form[key]:
                form[key] = spec["default"]           # left empty: the usual value
            try:
                n = int(form[key])
                ok = spec["min"] <= n <= spec["max"]
            except ValueError:
                ok = False
            if ok:
                form[key] = str(n)
            else:
                errors[key] = t["settings.error.int"] % {"min": spec["min"], "max": spec["max"]}
        if not errors:
            changed = []
            for key, value in form.items():
                before = get_setting(key, "") or ""
                if before != value:
                    changed.append("%s: %s → %s" % (key, before or "—", value or "—"))
                set_setting(key, value)
            if changed:
                log_action("UPDATE", "settings", None, " · ".join(changed))
            db.session.commit()
            flash("success|" + t["settings.saved"])
            return redirect(url_for("settings.index"))
    values = form or {k: get_setting(k, "") or "" for k in SETTINGS_SCHEMA}
    ports = Port.query.order_by(Port.is_active.desc(), Port.sort_order, Port.name).all()
    return render_template("settings.html", schema=SETTINGS_SCHEMA, groups=GROUPS,
                           values=values, errors=errors, ports=ports)


# ── Ports ────────────────────────────────────────────────────────────────────


def _port_or_404(pid):
    p = db.session.get(Port, pid)
    if p is None:
        abort(404)
    return p


def _save_port(port):
    t = get_t()
    name = (request.form.get("name") or "").strip()
    if not name:
        return t["port.error.name"]
    clash = Port.query.filter(db.func.lower(Port.name) == name.lower())
    if port.id:
        clash = clash.filter(Port.id != port.id)
    if clash.first():
        return t["port.error.exists"]
    port.name = name
    is_new = port.id is None
    if is_new:
        port.sort_order = (db.session.query(db.func.max(Port.sort_order)).scalar() or 0) + 1
        db.session.add(port)
        db.session.flush()
    log_action("CREATE" if is_new else "UPDATE", "port", port.id, port.name)
    db.session.commit()
    return None


@settings_bp.route("/ports/new", methods=["GET", "POST"])
@login_required
def port_new():
    port, error = Port(), None
    if request.method == "POST":
        error = _save_port(port)
        if error is None:
            flash("success|" + get_t()["port.saved"])
            return modal_ok() if is_modal_request() else redirect(url_for("settings.index"))
    return form_response("port_form.html", "_port_form.html", row=None, error=error)


@settings_bp.route("/ports/<int:pid>/edit", methods=["GET", "POST"])
@login_required
def port_edit(pid):
    port, error = _port_or_404(pid), None
    if request.method == "POST":
        error = _save_port(port)
        if error is None:
            flash("success|" + get_t()["port.saved"])
            return modal_ok() if is_modal_request() else redirect(url_for("settings.index"))
    return form_response("port_form.html", "_port_form.html", row=port, error=error)


@settings_bp.route("/ports/<int:pid>/archive", methods=["POST"])
@login_required
def port_archive(pid):
    port = _port_or_404(pid)
    port.is_active = not port.is_active
    log_action("RESTORE" if port.is_active else "ARCHIVE", "port", port.id, port.name)
    db.session.commit()
    flash("success|" + get_t()["archive.done" if not port.is_active else "archive.restored"])
    return redirect(url_for("settings.index"))


@settings_bp.route("/ports/<int:pid>/delete", methods=["POST"])
@login_required
def port_delete(pid):
    port = _port_or_404(pid)
    t = get_t()
    if port.has_history:
        flash("error|" + t["delete.has_history"])
    else:
        log_action("DELETE", "port", port.id, port.name)
        db.session.delete(port)
        db.session.commit()
        flash("success|" + t["delete.done"])
    return redirect(url_for("settings.index"))
