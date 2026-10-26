"""Utilisateurs et rôles — who can sign in, and what each may do.

Super admin only (access.SUPER_ONLY_BLUEPRINTS), as in zone_web: one page,
two tabs. Users: a username to sign in with, a name, an optional email, a
role, active or not. Roles: a name and a grid of areas, each at none, view
or manage (or no / yes) -- see access.AREAS.

The super admin is never created here and never loses access: their row
can only have its name and email changed. A user is deactivated rather than
deleted, so what they recorded keeps its author; a role still held by
someone cannot be deleted.
"""
import re

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from access import AREAS
from app import form_response, get_t, is_modal_request, modal_ok
from models import Role, User, db

users_bp = Blueprint("users", __name__, url_prefix="/parametres/utilisateurs")

USERNAME = re.compile(r"^[a-z0-9._-]{3,40}$")
MIN_PASSWORD = 8


def _tab_counts():
    return {"users": User.query.count(), "roles": Role.query.count()}


@users_bp.route("/")
@login_required
def index():
    users = User.query.order_by(User.is_super_admin.desc(), User.is_active.desc(), User.full_name).all()
    return render_template("users.html", users=users, tab_counts=_tab_counts())


# ── Users ────────────────────────────────────────────────────────────────────


def _get_user(uid):
    u = db.session.get(User, uid)
    if u is None:
        abort(404)
    return u


def _save_user(user, is_new):
    """Copy the form onto `user`. Returns an error message, or None."""
    t = get_t()
    f = request.form
    full_name = (f.get("full_name") or "").strip()
    email = (f.get("email") or "").strip().lower() or None
    password = f.get("password") or ""
    if not full_name:
        return t["user.error.name"]
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return t["user.error.email"]
    if is_new:
        username = (f.get("username") or "").strip().lower()
        if not USERNAME.match(username):
            return t["user.error.username"]
        if User.query.filter_by(username=username).first():
            return t["user.error.username_taken"]
        user.username = username
    if is_new or password:
        if len(password) < MIN_PASSWORD:
            return t["user.error.password"] % {"n": MIN_PASSWORD}
        if password != (f.get("password_confirm") or ""):
            return t["pw.error.no_match"]
        user.set_password(password)
    if not user.is_super_admin:
        role = db.session.get(Role, f.get("role_id", type=int) or 0)
        if role is None:
            return t["user.error.role"]
        user.role = role
        if user.id != current_user.id:
            user.is_active = f.get("is_active") == "1" if not is_new else True
    user.full_name = full_name[:120]
    user.email = email
    if is_new:
        user.lang = current_user.lang
        db.session.add(user)
    db.session.commit()
    return None


@users_bp.route("/new", methods=["GET", "POST"])
@login_required
def user_new():
    if not Role.query.count():
        flash("error|" + get_t()["user.error.no_roles"])
        return modal_ok() if is_modal_request() else redirect(url_for("users.roles"))
    error = None
    if request.method == "POST":
        user = User(is_super_admin=False, is_active=True)
        error = _save_user(user, is_new=True)
        if error is None:
            flash("success|" + get_t()["user.saved"] % {"name": user.full_name})
            return modal_ok() if is_modal_request() else redirect(url_for("users.index"))
        db.session.rollback()
    return form_response("user_form.html", "_user_form.html", row=None, error=error,
                         roles=Role.query.order_by(Role.name).all())


@users_bp.route("/<int:uid>/edit", methods=["GET", "POST"])
@login_required
def user_edit(uid):
    user, error = _get_user(uid), None
    if request.method == "POST":
        error = _save_user(user, is_new=False)
        if error is None:
            flash("success|" + get_t()["user.saved"] % {"name": user.full_name})
            return modal_ok() if is_modal_request() else redirect(url_for("users.index"))
        db.session.rollback()
        user = _get_user(uid)
    return form_response("user_form.html", "_user_form.html", row=user, error=error,
                         roles=Role.query.order_by(Role.name).all())


@users_bp.route("/<int:uid>/active", methods=["POST"])
@login_required
def user_toggle(uid):
    """Deactivate (they can no longer sign in, and are signed out at their
    next click) or reactivate. Never the super admin, never oneself."""
    user = _get_user(uid)
    t = get_t()
    if user.is_super_admin or user.id == current_user.id:
        flash("error|" + t["user.error.self"])
    else:
        user.is_active = not user.is_active
        db.session.commit()
        flash("success|" + (t["user.activated"] if user.is_active else t["user.deactivated"]) % {"name": user.full_name})
    return redirect(url_for("users.index"))


# ── Roles ────────────────────────────────────────────────────────────────────


@users_bp.route("/roles")
@login_required
def roles():
    rows = Role.query.order_by(Role.name).all()
    return render_template("roles.html", roles=rows, areas=AREAS, tab_counts=_tab_counts())


def _get_role(rid):
    r = db.session.get(Role, rid)
    if r is None:
        abort(404)
    return r


def _read_grid():
    """The grid as {area: level}, keeping only levels each area has."""
    perms = {}
    for area, levels in AREAS.items():
        v = request.form.get("perm_" + area, "none")
        if v in levels:
            perms[area] = v
    return perms


def _role_form(role):
    t = get_t()
    error = None
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        clash = Role.query.filter(db.func.lower(Role.name) == name.lower())
        if role.id:
            clash = clash.filter(Role.id != role.id)
        if not name:
            error = t["role.error.name"]
        elif clash.first():
            error = t["role.error.exists"]
        else:
            role.name = name[:60]
            role.description = (request.form.get("description") or "").strip()[:255] or None
            role.permissions = _read_grid()
            if role.id is None:
                db.session.add(role)
            db.session.commit()
            flash("success|" + t["role.saved"] % {"name": role.name})
            return redirect(url_for("users.roles"))
    state = _read_grid() if request.method == "POST" else dict(role.permissions or {})
    return render_template("role_form.html", row=role if role.id else None, error=error,
                           areas=AREAS, state=state)


@users_bp.route("/roles/new", methods=["GET", "POST"])
@login_required
def role_new():
    return _role_form(Role(permissions={}))


@users_bp.route("/roles/<int:rid>/edit", methods=["GET", "POST"])
@login_required
def role_edit(rid):
    return _role_form(_get_role(rid))


@users_bp.route("/roles/<int:rid>/delete", methods=["POST"])
@login_required
def role_delete(rid):
    role = _get_role(rid)
    t = get_t()
    if role.users:
        flash("error|" + t["role.error.in_use"] % {"n": len(role.users)})
    else:
        db.session.delete(role)
        db.session.commit()
        flash("success|" + t["delete.done"])
    return redirect(url_for("users.roles"))
