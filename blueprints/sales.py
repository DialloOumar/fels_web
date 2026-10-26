"""Ventes — sales and pre-orders, and the receipts paid against them.

A sale is numbered by the app, VT-YYYY-NNN, by the year of its date. It may
be taken before the vehicle is in stock -- a pre-order, against a deposit --
and a specific vehicle named on its line later. A vehicle sits on at most one
live sale at a time.

Its life: pré-commande → confirmée → livrée, or annulée. A numbered sale is
never deleted: a cancelled one stays in the register and frees its vehicles.
It can be cancelled only once its receipts are voided -- money received is
settled first, on paper, then here.

Each receipt is numbered REC-YYYY-NNN, in either currency at the rate of its
day, and has a printable page to hand over.
"""
from datetime import date
from decimal import Decimal

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError

from exports import wants_xlsx, xlsx_response

from app import (ListPage, current_rate, form_response, fr, get_setting, get_t,
                 is_modal_request, log_action, modal_ok, money_text, paginate,
                 parse_date, parse_money, rate_field_value)
from blueprints.payments import _parse_payment
from models import (CURRENCIES, PAYMENT_METHODS, Customer, Payment, Product,
                    Sale, SaleLine, Unit, convert, db, next_number)

sales_bp = Blueprint("sales", __name__, url_prefix="/ventes")

EDITABLE = ("preorder", "confirmed")
PAY_STATUSES = ("unpaid", "partial", "paid", "cancelled")


class FormError(Exception):
    pass


def _get_or_404(sid):
    s = db.session.get(Sale, sid)
    if s is None:
        abort(404)
    return s


def _commit_numbered(obj, column, prefix, attr):
    """Give `obj` the next number of its series and commit, retrying when
    another save took the same number in between."""
    for _ in range(5):
        setattr(obj, attr, next_number(column, prefix))
        try:
            db.session.commit()
            return
        except IntegrityError:
            db.session.rollback()
            db.session.add(obj)
    raise RuntimeError("could not allocate a number in %s" % prefix)


def _sale_label(sale):
    return "%s · %s · %s" % (sale.number, sale.customer.name, money_text(sale.total, sale.currency))


def _log_sale(action, sale, extra=None):
    log_action(action, "sale", sale.id, _sale_label(sale) + (" · " + extra if extra else ""))


# ── List ─────────────────────────────────────────────────────────────────────


@sales_bp.route("/")
@login_required
def index():
    search = (request.args.get("q") or "").strip()
    customer_id = request.args.get("customer", type=int)
    status = request.args.get("status") or ""
    pay = request.args.get("pay") or ""
    q = Sale.query.join(Customer)
    if customer_id:
        q = q.filter(Sale.customer_id == customer_id)
    if status in ("preorder", "confirmed", "delivered", "cancelled"):
        q = q.filter(Sale.status == status)
    else:
        status = ""
    if search:
        like = "%" + search + "%"
        q = (q.outerjoin(SaleLine).outerjoin(Product, SaleLine.product_id == Product.id)
             .outerjoin(Unit, SaleLine.unit_id == Unit.id)
             .filter(db.or_(Sale.number.ilike(like), Customer.name.ilike(like), Product.name.ilike(like),
                            Unit.chassis_number.ilike(like), Sale.note.ilike(like))).distinct())
    q = q.order_by(Sale.sale_date.desc(), Sale.id.desc())
    if pay not in PAY_STATUSES:
        pay = ""
    t = get_t()
    if wants_xlsx(request):
        rows = [s for s in q.all() if not pay or s.pay_status == pay]
        headers = [t["sale.number"], t["sale.customer"], t["sale.date"], t["sale.status"], t["sale.vehicles"],
                   t["purchase.currency"], t["sale.total"], t["sale.received"], t["sale.balance"],
                   t["purchase.payment"], t["sale.delivered_on"], t["field.note"]]
        data = [[s.number, s.customer.name, s.sale_date, t["sale.status." + s.status],
                 ", ".join(l.product.name + (" (" + l.unit.chassis_number + ")" if l.unit and l.unit.chassis_number else "")
                           for l in s.lines),
                 s.currency, s.total, s.paid_amount, s.balance, t["pay." + s.pay_status], s.delivered_on, s.note]
                for s in rows]
        return xlsx_response("ventes", t["nav.sales"], headers, data)
    if pay:
        pagination = ListPage([s for s in q.all() if s.pay_status == pay])
    else:
        pagination = paginate(q)
    opts = {
        "customers": [(c.id, c.name) for c in Customer.query.order_by(Customer.name)],
        "statuses": [(s, t["sale.status." + s]) for s in ("preorder", "confirmed", "delivered", "cancelled")],
        "pay": [(s, t["pay." + s]) for s in PAY_STATUSES if s != "cancelled"],
    }
    return render_template("sales.html", rows=pagination.items, pagination=pagination, search=search,
                           customer_id=customer_id, status=status, pay=pay, opts=opts)


# ── Create / edit ────────────────────────────────────────────────────────────


def _parse_lines(sale):
    t = get_t()
    f = request.form
    prods, units = f.getlist("line_product"), f.getlist("line_unit")
    prices, descs = f.getlist("line_price"), f.getlist("line_desc")
    rows, seen_units = [], set()
    for i in range(len(prods)):
        pid = (prods[i] or "").strip()
        uid = (units[i] if i < len(units) else "").strip()
        raw_p = (prices[i] if i < len(prices) else "").strip()
        desc = (descs[i] if i < len(descs) else "").strip()
        if not pid and not uid and not raw_p and not desc:
            continue
        product = db.session.get(Product, int(pid)) if pid.isdigit() else None
        if product is None:
            raise FormError(t["sale.error.line_product"] % {"n": i + 1})
        unit = None
        if uid:
            unit = db.session.get(Unit, int(uid)) if uid.isdigit() else None
            if unit is None:
                raise FormError(t["sale.error.unit"] % {"n": i + 1})
            if unit.product_id != product.id:
                raise FormError(t["sale.error.unit_model"] % {"n": i + 1})
            if unit.id in seen_units:
                raise FormError(t["sale.error.unit_twice"] % {"n": i + 1})
            other = unit.active_sale_line
            if other is not None and other.sale_id != sale.id:
                raise FormError(t["sale.error.unit_taken"] % {"n": i + 1, "sale": other.sale.number})
            seen_units.add(unit.id)
        price = parse_money(raw_p) if raw_p else None
        if price is None or price < 0:
            raise FormError(t["sale.error.line_price"] % {"n": i + 1})
        rows.append({"product": product, "unit": unit, "price": price, "description": desc[:255] or None})
    if not rows:
        raise FormError(t["sale.error.no_lines"])
    return rows


def _save(sale):
    t = get_t()
    f = request.form
    customer = db.session.get(Customer, f.get("customer_id", type=int) or 0)
    if customer is None or (not customer.is_active and customer.id != sale.customer_id):
        raise FormError(t["sale.error.customer"])
    sale_date = parse_date(f.get("sale_date"))
    if sale_date is None:
        raise FormError(t["form.error.date"])
    currency = f.get("currency")
    if currency not in CURRENCIES:
        raise FormError(t["form.error.currency"])
    status = f.get("status") or "preorder"
    if status not in EDITABLE:
        raise FormError(t["sale.error.status"])
    if sale.id and sale.status not in EDITABLE:
        raise FormError(t["sale.error.locked"])
    if sale.id and currency != sale.currency and any(not p.is_voided for p in sale.payments):
        raise FormError(t["sale.error.currency_locked"])
    rows = _parse_lines(sale)

    sale.customer = customer
    sale.sale_date = sale_date
    sale.currency = currency
    sale.status = status
    sale.note = (f.get("note") or "").strip() or None
    # Lines are rewritten as typed: they carry no history of their own.
    for line in list(sale.lines):
        sale.lines.remove(line)
    db.session.flush()
    for r in rows:
        sale.lines.append(SaleLine(product=r["product"], unit=r["unit"], price=r["price"],
                                   description=r["description"]))
    if sale.id is None:
        sale.created_by = current_user.id
        db.session.add(sale)
        _commit_numbered(sale, Sale.number, "VT-%d-" % sale_date.year, "number")
        _log_sale("CREATE", sale)
    else:
        db.session.flush()
        _log_sale("UPDATE", sale)
    db.session.commit()


def _form_context(sale):
    """Customers, models, and the vehicles that can go on a line: free ones,
    plus those already on this sale."""
    customers = Customer.query.filter(db.or_(Customer.is_active.is_(True), Customer.id == (sale.customer_id or 0))
                                      ).order_by(Customer.name).all()
    products = Product.query.filter(db.or_(
        Product.is_active.is_(True), Product.id.in_([l.product_id for l in sale.lines] or [0]))
    ).order_by(Product.category, Product.name).all()
    own = {l.unit_id for l in sale.lines if l.unit_id}
    units = [u for u in Unit.query.order_by(Unit.product_id, Unit.id).all()
             if u.active_sale_line is None or u.id in own]
    return {"customers": customers, "products": products, "units": units}


@sales_bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    sale, error = Sale(), None
    if request.method == "POST":
        try:
            _save(sale)
            flash("success|" + get_t()["sale.saved"] % {"number": sale.number})
            return redirect(url_for("sales.detail", sid=sale.id))
        except FormError as e:
            db.session.rollback()
            error = str(e)
        sale = Sale()
    preset = {"customer": request.args.get("customer", type=int), "unit": request.args.get("unit", type=int)}
    preset_unit = db.session.get(Unit, preset["unit"]) if preset["unit"] else None
    return render_template("sale_form.html", row=None, error=error, preset=preset,
                           preset_unit=preset_unit, **_form_context(sale))


@sales_bp.route("/<int:sid>/edit", methods=["GET", "POST"])
@login_required
def edit(sid):
    sale, error = _get_or_404(sid), None
    if sale.status not in EDITABLE:
        flash("error|" + get_t()["sale.error.locked"])
        return redirect(url_for("sales.detail", sid=sid))
    if request.method == "POST":
        try:
            _save(sale)
            flash("success|" + get_t()["sale.saved"] % {"number": sale.number})
            return redirect(url_for("sales.detail", sid=sale.id))
        except FormError as e:
            db.session.rollback()
            error = str(e)
            sale = _get_or_404(sid)
    return render_template("sale_form.html", row=sale, error=error, preset={}, preset_unit=None,
                           **_form_context(sale))


def line_margin(line, sale_currency):
    """(margin, landed cost in the sale's currency) for a line whose vehicle
    is known, at today's rate when the currencies differ; None otherwise.
    An estimate for the eye -- the books are the receipts and the costs."""
    if line.unit is None:
        return None
    lc = line.unit.landed_cost
    if lc is None:
        return None
    landed = convert(lc[0], lc[1], sale_currency, current_rate())
    if landed is None:
        return None
    return (Decimal(line.price or 0) - landed, landed)


@sales_bp.route("/<int:sid>")
@login_required
def detail(sid):
    sale = _get_or_404(sid)
    margins = {l.id: line_margin(l, sale.currency) for l in sale.lines}
    known = [m for m in margins.values() if m is not None]
    total_margin = sum((m[0] for m in known), Decimal("0")) if known and len(known) == len(sale.lines) else None
    return render_template("sale_detail.html", row=sale, margins=margins, total_margin=total_margin,
                           editable=sale.status in EDITABLE)


# ── Life: confirm, deliver, cancel ───────────────────────────────────────────


@sales_bp.route("/<int:sid>/confirmer", methods=["POST"])
@login_required
def confirm(sid):
    sale = _get_or_404(sid)
    if sale.status == "preorder":
        sale.status = "confirmed"
        _log_sale("CONFIRM", sale)
        db.session.commit()
        flash("success|" + get_t()["sale.confirmed"])
    return redirect(url_for("sales.detail", sid=sid))


@sales_bp.route("/<int:sid>/livrer", methods=["GET", "POST"])
@login_required
def deliver(sid):
    """Hand the vehicles over, on a date. Every line must name its vehicle:
    what was delivered has to be known."""
    sale = _get_or_404(sid)
    t = get_t()
    error = None
    if sale.status not in EDITABLE:
        error = t["sale.error.locked"]
    elif any(l.unit_id is None for l in sale.lines):
        error = t["sale.error.deliver_units"]
    if request.method == "POST" and error is None:
        when = parse_date(request.form.get("delivered_on"))
        if when is None:
            error = t["form.error.date"]
        else:
            sale.status = "delivered"
            sale.delivered_on = when
            _log_sale("DELIVER", sale, when.strftime("%d/%m/%Y"))
            db.session.commit()
            flash("success|" + t["sale.delivered"])
            if sale.balance > 0:
                flash("warning|" + t["sale.warn.delivered_unpaid"])
            return modal_ok() if is_modal_request() else redirect(url_for("sales.detail", sid=sid))
    return form_response("deliver_form.html", "_deliver_form.html", row=sale, error=error,
                         today=date.today().isoformat())


@sales_bp.route("/<int:sid>/annuler-livraison", methods=["POST"])
@login_required
def undeliver(sid):
    """A delivery recorded by mistake: back to confirmed."""
    sale = _get_or_404(sid)
    if sale.status == "delivered":
        sale.status, sale.delivered_on = "confirmed", None
        _log_sale("UNDELIVER", sale)
        db.session.commit()
        flash("success|" + get_t()["sale.undelivered"])
    return redirect(url_for("sales.detail", sid=sid))


@sales_bp.route("/<int:sid>/annuler", methods=["POST"])
@login_required
def cancel(sid):
    sale = _get_or_404(sid)
    t = get_t()
    if sale.status == "delivered":
        flash("error|" + t["sale.error.cancel_delivered"])
    elif any(not p.is_voided for p in sale.payments):
        flash("error|" + t["sale.error.cancel_paid"])
    elif sale.status != "cancelled":
        sale.status = "cancelled"
        _log_sale("CANCEL", sale)
        db.session.commit()
        flash("success|" + t["sale.cancelled"])
    return redirect(url_for("sales.detail", sid=sid))


# ── Receipts ─────────────────────────────────────────────────────────────────


@sales_bp.route("/<int:sid>/recu/nouveau", methods=["GET", "POST"])
@login_required
def receipt_new(sid):
    sale = _get_or_404(sid)
    t = get_t()
    error = None
    if sale.status == "cancelled":
        error = t["sale.error.cancelled_receipt"]
    if request.method == "POST" and error is None:
        values, error = _parse_payment(sale.currency)
        if error is None:
            p = Payment(direction="in", sale_id=sale.id, created_by=current_user.id, **values)
            db.session.add(p)
            _commit_numbered(p, Payment.receipt_number, "REC-%d-" % values["paid_on"].year, "receipt_number")
            log_action("CREATE", "receipt", p.id, "%s · %s · %s · %s" % (
                p.receipt_number, sale.number, money_text(p.amount, p.currency), fr("method." + p.method)))
            db.session.commit()
            flash("success|" + t["receipt.saved"] % {"number": p.receipt_number})
            in_sale = convert(values["amount"], values["currency"], sale.currency, values["rate"])
            db.session.refresh(sale)
            if in_sale is not None and sale.paid_amount > sale.total + Decimal("0.01"):
                flash("warning|" + t["receipt.warn.over_total"])
            return modal_ok() if is_modal_request() else redirect(url_for("sales.detail", sid=sid))
    return form_response("payment_form.html", "_payment_form.html", error=error,
                         target_currency=sale.currency, balance=sale.balance,
                         default_rate=rate_field_value(current_rate()),
                         methods=PAYMENT_METHODS, form_action=url_for("sales.receipt_new", sid=sid),
                         back_url=url_for("sales.detail", sid=sid), title=t["receipt.new"],
                         default_method="cash")


@sales_bp.route("/recu/<int:pay_id>")
@login_required
def receipt_print(pay_id):
    """The receipt to hand over: who paid, how much, for what, and what is
    left -- read as of this receipt, so a reprint later says the same."""
    p = db.session.get(Payment, pay_id) or abort(404)
    if p.direction != "in" or p.sale is None:
        abort(404)
    sale = p.sale
    earlier = [x for x in sale.payments if not x.is_voided and (x.paid_on, x.id) <= (p.paid_on, p.id)]
    paid_to_date = sum((x.amount_in(sale.currency) or Decimal("0") for x in earlier), Decimal("0"))
    left = max(sale.total - paid_to_date, Decimal("0"))
    company = {k: get_setting("company_" + k, "") for k in ("name", "phone", "email", "address")}
    return render_template("receipt_print.html", p=p, sale=sale, paid_to_date=paid_to_date, left=left,
                           company=company, footer=get_setting("receipt_footer", ""))
