"""Stock — every vehicle, one row each.

A vehicle is born from a purchase line. Where it is on the road comes from
its purchase's stage; whether it is free, reserved, sold or delivered comes from
the sales naming it. Both are read, never stored."""
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from exports import wants_xlsx, xlsx_response

from app import ListPage, current_rate, form_response, get_t, is_modal_request, log_action, modal_ok
from models import (PURCHASE_STAGES, AuditLog, Port, Product, Purchase, PurchaseLine, Supplier, Unit,
                    CENT, ZERO, convert, db)

stock_bp = Blueprint("stock", __name__, url_prefix="/stock")

UNIT_STATUSES = ("available", "reserved", "sold", "delivered")
# Not in the menu: what a sale holds, reserved or sold -- the dashboard's and
# the catalogue's "reserved or sold" figures open the list on it.
HELD = ("reserved", "sold")
# Where on the road: still travelling, or in the yard.
WHERE = ("incoming", "in_stock")


def tab_counts():
    """The numbers on the Vehicles page's tabs: every vehicle, and the
    models of the catalog still in use."""
    return {"units": Unit.query.count(), "products": Product.query.filter_by(is_active=True).count()}


@stock_bp.route("/")
@login_required
def index():
    search = (request.args.get("q") or "").strip()
    product_id = request.args.get("product", type=int)
    port_id = request.args.get("port", type=int)
    status = request.args.get("status") or ""
    where = request.args.get("where") or ""
    q = (Unit.query.join(PurchaseLine).join(Purchase).join(Supplier).join(Product, Unit.product_id == Product.id))
    if product_id:
        q = q.filter(Unit.product_id == product_id)
    if port_id:
        q = q.filter(Purchase.port_id == port_id)
    if where == "in_stock":
        q = q.filter(Purchase.stage == "in_stock")
    elif where == "incoming":
        q = q.filter(Purchase.stage != "in_stock")
    else:
        where = ""
    if search:
        like = "%" + search + "%"
        q = q.filter(db.or_(Unit.ref.ilike(like), Unit.chassis_number.ilike(like), Purchase.invoice_number.ilike(like),
                            Product.name.ilike(like), Supplier.name.ilike(like)))
    rows = q.order_by(Purchase.invoice_date.desc(), Unit.ref, Unit.id).all()
    counts = {s: 0 for s in UNIT_STATUSES}
    for u in rows:
        counts[u.status] += 1
    if status in UNIT_STATUSES:
        rows = [u for u in rows if u.status == status]
    elif status == "held":
        rows = [u for u in rows if u.status in HELD]
    else:
        status = ""
    t = get_t()
    if wants_xlsx(request):
        landed = _landed_costs(rows)
        headers = [t["unit.ref"], t["purchase.line_product"], t["unit.chassis"], t["unit.purchase"], t["purchase.supplier"],
                   t["purchase.port"], t["purchase.stage"], t["unit.status"], t["sale.number"], t["sale.customer"],
                   t["purchase.landed_unit"], t["purchase.currency"], t["field.note"]]
        data = []
        for u in rows:
            p, sl, lc = u.purchase_line.purchase, u.active_sale_line, landed.get(u.id)
            data.append([u.ref, u.product.name, u.chassis_number, p.invoice_number, p.supplier.name,
                         p.port.name if p.port else None, t["stage." + p.stage], t["unit.status." + u.status],
                         sl.sale.number if sl else None, sl.sale.customer.name if sl else None,
                         lc[0] if lc else None, lc[1] if lc else None, u.note])
        return xlsx_response("vehicules", t["nav.stock"], headers, data)
    pagination = ListPage(rows)
    opts = {
        "products": [(p.id, p.name) for p in Product.query.order_by(Product.category, Product.name)],
        "ports": [(p.id, p.name) for p in Port.query.order_by(Port.sort_order, Port.name)],
        "statuses": [(s, "%s (%d)" % (t["unit.status." + s], counts[s])) for s in UNIT_STATUSES],
        "wheres": [(w, t["unit.where." + w]) for w in WHERE],
    }
    return render_template(
        "stock.html", rows=pagination.items, pagination=pagination, search=search,
        product_id=product_id, port_id=port_id, status=status, where=where, opts=opts,
        landed=_landed_costs(pagination.items), tab_counts=tab_counts())


def clean_chassis(entries, t, required=False):
    """Read chassis numbers typed for several vehicles at once.

    `entries` is [(unit, typed)]. Returns ({unit: number or None}, error):
    upper-cased, never twice in the form, never a number another vehicle
    already carries. With `required`, none may be left empty."""
    out, seen = {}, {}
    for u, typed in entries:
        number = (typed or "").strip().upper()[:60] or None
        if number is None and required:
            return {}, t["unit.error.chassis_required"] % {"ref": u.ref}
        if number:
            if number in seen:
                return {}, t["unit.error.chassis_twice"] % {"number": number}
            seen[number] = u
            ids = [x.id for x, _ in entries]
            clash = Unit.query.filter(db.func.upper(Unit.chassis_number) == number, Unit.id.notin_(ids)).first()
            if clash:
                return {}, t["unit.error.chassis_taken"] % {"number": number, "ref": clash.ref}
        out[u] = number
    return out, None


def save_chassis(numbers):
    """Write what clean_chassis returned, journalling each change."""
    for u, number in numbers.items():
        if (u.chassis_number or None) != number:
            u.chassis_number = number
            log_action("UPDATE", "unit", u.id, "%s · %s · %s" % (
                u.product.name, number or "sans châssis", u.purchase_line.purchase.invoice_number))


def _landed_costs(units):
    """{unit_id: (landed cost, currency) or None}, a purchase's cost split
    computed once for all its vehicles on the page."""
    out, cache = {}, {}
    for u in units:
        p = u.purchase_line.purchase
        if p.id not in cache:
            cache[p.id] = p.extra_cost_per_unit()
        shares = cache[p.id]
        out[u.id] = None if shares is None else (u.purchase_line.unit_price + shares.get(u.purchase_line_id, 0), p.currency)
    return out


@stock_bp.route("/<int:uid>")
@login_required
def detail(uid):
    """One vehicle, all of it: the purchase it came on and what it cost
    landed, the road it took, the sale that names it and what was received,
    the papers, and every sale it was ever on."""
    unit = db.session.get(Unit, uid) or abort(404)
    line = unit.purchase_line
    purchase = line.purchase
    cur = purchase.currency
    shares = purchase.extra_cost_per_unit()
    share = shares.get(line.id, ZERO) if shares is not None else None
    landed = (line.unit_price + share) if share is not None else None
    # Each extra cost's part carried by this vehicle, spread as in
    # extra_cost_per_unit: in proportion to the price.
    goods = purchase.goods_total
    weight = (line.unit_price / goods) if goods > 0 else ZERO
    cost_parts = []
    for c in purchase.costs:
        v = c.amount_in(cur)
        cost_parts.append((c, None if v is None else (v * weight).quantize(CENT)))
    # Every sale that ever named it, the one in force first.
    sale_lines = sorted(unit.sale_lines, key=lambda sl: (sl.sale.status == "cancelled", sl.sale.sale_date, sl.id))
    active = unit.active_sale_line
    sale = active.sale if active else None
    margin = None
    if active and landed is not None:
        landed_in_sale = convert(landed, cur, sale.currency, current_rate())
        if landed_in_sale is not None:
            margin = (active.price - landed_in_sale, landed_in_sale)
    # The journal of what touched it: the vehicle, its purchase, its sales.
    history = []
    if current_user.is_super_admin:
        sale_ids = [sl.sale_id for sl in unit.sale_lines]
        conds = [db.and_(AuditLog.resource_type == "unit", AuditLog.resource_id == unit.id),
                 db.and_(AuditLog.resource_type == "purchase", AuditLog.resource_id == purchase.id)]
        if sale_ids:
            conds.append(db.and_(AuditLog.resource_type == "sale", AuditLog.resource_id.in_(sale_ids)))
        history = AuditLog.query.filter(db.or_(*conds)).order_by(AuditLog.timestamp.desc()).limit(100).all()
    return render_template(
        "unit_detail.html", row=unit, line=line, purchase=purchase, share=share, landed=landed,
        cost_parts=cost_parts, sale_lines=sale_lines, active=active, sale=sale, margin=margin,
        history=history, stages=PURCHASE_STAGES)


@stock_bp.route("/<int:uid>/edit", methods=["GET", "POST"])
@login_required
def edit(uid):
    """A vehicle's chassis number and note: what is learnt once it is seen."""
    unit = db.session.get(Unit, uid) or abort(404)
    t = get_t()
    error = None
    if request.method == "POST":
        note = (request.form.get("note") or "").strip()[:255] or None
        purchase = unit.purchase_line.purchase
        numbers, error = clean_chassis([(unit, request.form.get("chassis_number"))], t,
                                       required=chassis_required(purchase.stage))
        if error is None:
            unit.chassis_number = numbers[unit]
            unit.note = note
            log_action("UPDATE", "unit", unit.id, "%s · %s · %s" % (
                unit.product.name, unit.chassis_number or "sans châssis", purchase.invoice_number))
            db.session.commit()
            flash("success|" + t["unit.saved"])
            back = request.form.get("back") or url_for("stock.index")
            if not back.startswith("/") or back.startswith("//"):
                back = url_for("stock.index")
            return modal_ok() if is_modal_request() else redirect(back)
    return form_response("unit_form.html", "_unit_form.html", row=unit, error=error,
                         required=chassis_required(unit.purchase_line.purchase.stage),
                         back=request.args.get("back") or "")


def chassis_required(stage):
    """Past "shipped" every vehicle has its chassis number: it is on the
    bill of lading, and customs will not clear a vehicle without it."""
    return PURCHASE_STAGES.index(stage) > PURCHASE_STAGES.index(CHASSIS_GATE)


CHASSIS_GATE = "shipped"
