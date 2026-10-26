"""Fournisseurs — who the vehicles are bought from.

A supplier that appears on a purchase is archived, never deleted, so the
purchase keeps its name. Its page lists its purchases and what is still
owed on them."""
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import login_required

from exports import wants_xlsx, xlsx_response

from app import form_response, get_t, is_modal_request, log_action, modal_ok, paginate
from models import CURRENCIES, Supplier, db

suppliers_bp = Blueprint("suppliers", __name__, url_prefix="/fournisseurs")

FIELDS = ("name", "country", "contact_name", "phone", "email", "address", "note")


def _get_or_404(sid):
    s = db.session.get(Supplier, sid)
    if s is None:
        abort(404)
    return s


@suppliers_bp.route("/")
@login_required
def index():
    show_archived = request.args.get("archived") == "1"
    search = (request.args.get("q") or "").strip()
    q = Supplier.query.filter_by(is_active=not show_archived)
    if search:
        like = "%" + search + "%"
        q = q.filter(db.or_(Supplier.name.ilike(like), Supplier.country.ilike(like),
                            Supplier.contact_name.ilike(like), Supplier.phone.ilike(like)))
    if wants_xlsx(request):
        t = get_t()
        headers = [t["supplier.name"], t["supplier.country"], t["supplier.contact"], t["field.phone"],
                   t["field.email"], t["field.address"], t["supplier.default_currency"], t["field.note"]]
        data = [[s.name, s.country, s.contact_name, s.phone, s.email, s.address, s.default_currency, s.note]
                for s in q.order_by(Supplier.name)]
        return xlsx_response("fournisseurs", t["nav.suppliers"], headers, data)
    pagination = paginate(q.order_by(Supplier.name))
    archived_count = 0 if show_archived else Supplier.query.filter_by(is_active=False).count()
    return render_template("suppliers.html", rows=pagination.items, pagination=pagination,
                           search=search, show_archived=show_archived,
                           archived_count=archived_count)


def _save(row):
    """Copy the form onto `row`. Returns an error message, or None."""
    t = get_t()
    data = {f: (request.form.get(f) or "").strip() or None for f in FIELDS}
    currency = request.form.get("default_currency") or "USD"
    if not data["name"]:
        return t["supplier.error.name"]
    if currency not in CURRENCIES:
        return t["form.error.currency"]
    clash = Supplier.query.filter(db.func.lower(Supplier.name) == data["name"].lower())
    if row.id:
        clash = clash.filter(Supplier.id != row.id)
    if clash.first():
        return t["supplier.error.exists"]
    for f, v in data.items():
        setattr(row, f, v)
    row.default_currency = currency
    is_new = row.id is None
    if is_new:
        db.session.add(row)
        db.session.flush()
    log_action("CREATE" if is_new else "UPDATE", "supplier", row.id, row.name)
    db.session.commit()
    return None


@suppliers_bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    row, error = Supplier(default_currency="USD"), None
    if request.method == "POST":
        error = _save(row)
        if error is None:
            flash("success|" + get_t()["supplier.saved"])
            return modal_ok() if is_modal_request() else redirect(url_for("suppliers.detail", sid=row.id))
    return form_response("supplier_form.html", "_supplier_form.html", row=None, error=error)


@suppliers_bp.route("/<int:sid>")
@login_required
def detail(sid):
    """The supplier, its purchases, and what is still owed -- one total per
    currency, since a USD invoice and a GNF one do not add up."""
    row = _get_or_404(sid)
    owed, bought = {}, {}
    for p in row.purchases:
        bought[p.currency] = bought.get(p.currency, 0) + p.goods_total
        owed[p.currency] = owed.get(p.currency, 0) + p.balance
    return render_template("supplier_detail.html", row=row, owed=owed, bought=bought)


@suppliers_bp.route("/<int:sid>/edit", methods=["GET", "POST"])
@login_required
def edit(sid):
    row, error = _get_or_404(sid), None
    if request.method == "POST":
        error = _save(row)
        if error is None:
            flash("success|" + get_t()["supplier.saved"])
            return modal_ok() if is_modal_request() else redirect(url_for("suppliers.detail", sid=row.id))
    return form_response("supplier_form.html", "_supplier_form.html", row=row, error=error)


@suppliers_bp.route("/<int:sid>/archive", methods=["POST"])
@login_required
def archive(sid):
    row = _get_or_404(sid)
    row.is_active = False
    log_action("ARCHIVE", "supplier", row.id, row.name)
    db.session.commit()
    flash("success|" + get_t()["archive.done"])
    return redirect(url_for("suppliers.index"))


@suppliers_bp.route("/<int:sid>/reactivate", methods=["POST"])
@login_required
def reactivate(sid):
    row = _get_or_404(sid)
    row.is_active = True
    log_action("RESTORE", "supplier", row.id, row.name)
    db.session.commit()
    flash("success|" + get_t()["archive.restored"])
    return redirect(url_for("suppliers.index", archived=1))


@suppliers_bp.route("/<int:sid>/delete", methods=["POST"])
@login_required
def delete(sid):
    row = _get_or_404(sid)
    t = get_t()
    if row.has_history:
        flash("error|" + t["delete.has_history"])
        return redirect(url_for("suppliers.index", archived=1))
    log_action("DELETE", "supplier", row.id, row.name)
    db.session.delete(row)
    db.session.commit()
    flash("success|" + t["delete.done"])
    return redirect(url_for("suppliers.index", archived=1))
