"""Clients — who buys the vehicles.

Names are not unique (two customers may share one), so the form only warns
when a name is already taken. A customer with a sale is archived, never
deleted. Its page lists its sales and what it still owes, with a printable
statement."""
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import login_required

from exports import wants_xlsx, xlsx_response

from app import form_response, get_t, is_modal_request, log_action, modal_ok, paginate
from models import Customer, db

customers_bp = Blueprint("customers", __name__, url_prefix="/clients")

FIELDS = ("name", "phone", "email", "address", "tax_id", "note")


def _get_or_404(cid):
    c = db.session.get(Customer, cid)
    if c is None:
        abort(404)
    return c


@customers_bp.route("/")
@login_required
def index():
    show_archived = request.args.get("archived") == "1"
    search = (request.args.get("q") or "").strip()
    q = Customer.query.filter_by(is_active=not show_archived)
    if search:
        like = "%" + search + "%"
        q = q.filter(db.or_(Customer.name.ilike(like), Customer.phone.ilike(like),
                            Customer.address.ilike(like)))
    if wants_xlsx(request):
        t = get_t()
        headers = [t["customer.name"], t["field.phone"], t["field.email"], t["field.address"], t["field.note"]]
        data = [[c.name, c.phone, c.email, c.address, c.note] for c in q.order_by(Customer.name)]
        return xlsx_response("clients", t["nav.customers"], headers, data)
    pagination = paginate(q.order_by(Customer.name))
    archived_count = 0 if show_archived else Customer.query.filter_by(is_active=False).count()
    return render_template("customers.html", rows=pagination.items, pagination=pagination,
                           search=search, show_archived=show_archived,
                           archived_count=archived_count)


def _save(row):
    """Copy the form onto `row`. Returns (error, warning)."""
    t = get_t()
    data = {f: (request.form.get(f) or "").strip() or None for f in FIELDS}
    if not data["name"]:
        return t["customer.error.name"], None
    same = Customer.query.filter(db.func.lower(Customer.name) == data["name"].lower())
    if row.id:
        same = same.filter(Customer.id != row.id)
    warning = t["customer.warn.same_name"] if same.first() else None
    for f, v in data.items():
        setattr(row, f, v)
    is_new = row.id is None
    if is_new:
        db.session.add(row)
        db.session.flush()
    log_action("CREATE" if is_new else "UPDATE", "customer", row.id, row.name)
    db.session.commit()
    return None, warning


def _saved(row, warning):
    flash("success|" + get_t()["customer.saved"])
    if warning:
        flash("warning|" + warning)
    return modal_ok() if is_modal_request() else redirect(url_for("customers.detail", cid=row.id))


@customers_bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    row, error = Customer(), None
    if request.method == "POST":
        error, warning = _save(row)
        if error is None:
            return _saved(row, warning)
    return form_response("customer_form.html", "_customer_form.html", row=None, error=error)


def balances(customer):
    """What the customer still owes and has bought, one total per currency
    (a USD sale and a GNF one do not add up). Cancelled sales count for
    neither."""
    owed, bought = {}, {}
    for s in customer.sales:
        if s.status == "cancelled":
            continue
        bought[s.currency] = bought.get(s.currency, 0) + s.total
        owed[s.currency] = owed.get(s.currency, 0) + s.balance
    return owed, bought


@customers_bp.route("/<int:cid>")
@login_required
def detail(cid):
    row = _get_or_404(cid)
    owed, bought = balances(row)
    return render_template("customer_detail.html", row=row, owed=owed, bought=bought)


@customers_bp.route("/<int:cid>/releve")
@login_required
def statement(cid):
    """The customer's statement, to print: every sale and every receipt in
    date order, one running balance per currency. A receipt counts in its
    sale's currency, at its own rate, as it does on the sale."""
    from app import get_setting
    row = _get_or_404(cid)
    entries = {}
    for s in row.sales:
        if s.status == "cancelled":
            continue
        entries.setdefault(s.currency, []).append(
            {"date": s.sale_date, "order": (s.sale_date, 0, s.id), "doc": s.number, "kind": "sale",
             "label": ", ".join(l.product.name for l in s.lines), "debit": s.total, "credit": None})
        for p in s.payments:
            if p.is_voided:
                continue
            entries[s.currency].append(
                {"date": p.paid_on, "order": (p.paid_on, 1, p.id), "doc": p.receipt_number, "kind": "receipt",
                 "label": s.number, "debit": None, "credit": p.amount_in(s.currency) or 0,
                 "original": (p.amount, p.currency) if p.currency != s.currency else None})
    for cur, rows in entries.items():
        rows.sort(key=lambda e: e["order"])
        running = 0
        for e in rows:
            running += (e["debit"] or 0) - (e["credit"] or 0)
            e["balance"] = running
    company = {k: get_setting("company_" + k, "") for k in ("name", "phone", "email", "address")}
    return render_template("statement_print.html", row=row, entries=entries, company=company)


@customers_bp.route("/<int:cid>/edit", methods=["GET", "POST"])
@login_required
def edit(cid):
    row, error = _get_or_404(cid), None
    if request.method == "POST":
        error, warning = _save(row)
        if error is None:
            return _saved(row, warning)
    return form_response("customer_form.html", "_customer_form.html", row=row, error=error)


@customers_bp.route("/<int:cid>/archive", methods=["POST"])
@login_required
def archive(cid):
    row = _get_or_404(cid)
    row.is_active = False
    log_action("ARCHIVE", "customer", row.id, row.name)
    db.session.commit()
    flash("success|" + get_t()["archive.done"])
    return redirect(url_for("customers.index"))


@customers_bp.route("/<int:cid>/reactivate", methods=["POST"])
@login_required
def reactivate(cid):
    row = _get_or_404(cid)
    row.is_active = True
    log_action("RESTORE", "customer", row.id, row.name)
    db.session.commit()
    flash("success|" + get_t()["archive.restored"])
    return redirect(url_for("customers.index", archived=1))


@customers_bp.route("/<int:cid>/delete", methods=["POST"])
@login_required
def delete(cid):
    row = _get_or_404(cid)
    t = get_t()
    if row.has_history:
        flash("error|" + t["delete.has_history"])
        return redirect(url_for("customers.index", archived=1))
    log_action("DELETE", "customer", row.id, row.name)
    db.session.delete(row)
    db.session.commit()
    flash("success|" + t["delete.done"])
    return redirect(url_for("customers.index", archived=1))
