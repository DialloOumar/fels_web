"""Stock — every vehicle, one row each.

A vehicle is born from a purchase line. Where it is on the road comes from
its purchase's stage; whether it is free, reserved or delivered comes from
the sales naming it. Both are read, never stored."""
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import login_required

from exports import wants_xlsx, xlsx_response

from app import ListPage, form_response, get_t, is_modal_request, log_action, modal_ok
from models import Port, Product, Purchase, PurchaseLine, Supplier, Unit, db

stock_bp = Blueprint("stock", __name__, url_prefix="/stock")

UNIT_STATUSES = ("available", "reserved", "delivered")
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
        q = q.filter(db.or_(Unit.chassis_number.ilike(like), Purchase.invoice_number.ilike(like),
                            Product.name.ilike(like), Supplier.name.ilike(like)))
    rows = q.order_by(Purchase.invoice_date.desc(), Unit.id).all()
    counts = {s: 0 for s in UNIT_STATUSES}
    for u in rows:
        counts[u.status] += 1
    if status in UNIT_STATUSES:
        rows = [u for u in rows if u.status == status]
    else:
        status = ""
    t = get_t()
    if wants_xlsx(request):
        landed = _landed_costs(rows)
        headers = [t["purchase.line_product"], t["unit.chassis"], t["unit.purchase"], t["purchase.supplier"],
                   t["purchase.port"], t["purchase.stage"], t["unit.status"], t["sale.number"], t["sale.customer"],
                   t["purchase.landed_unit"], t["purchase.currency"], t["field.note"]]
        data = []
        for u in rows:
            p, sl, lc = u.purchase_line.purchase, u.active_sale_line, landed.get(u.id)
            data.append([u.product.name, u.chassis_number, p.invoice_number, p.supplier.name,
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


@stock_bp.route("/<int:uid>/edit", methods=["GET", "POST"])
@login_required
def edit(uid):
    """A vehicle's chassis number and note: what is learnt once it is seen."""
    unit = db.session.get(Unit, uid) or abort(404)
    t = get_t()
    error = None
    if request.method == "POST":
        chassis = (request.form.get("chassis_number") or "").strip().upper() or None
        note = (request.form.get("note") or "").strip()[:255] or None
        if chassis:
            clash = Unit.query.filter(db.func.upper(Unit.chassis_number) == chassis, Unit.id != unit.id).first()
            if clash:
                error = t["unit.error.chassis_exists"]
        if error is None:
            unit.chassis_number = chassis[:60] if chassis else None
            unit.note = note
            log_action("UPDATE", "unit", unit.id, "%s · %s · %s" % (
                unit.product.name, unit.chassis_number or "sans châssis", unit.purchase_line.purchase.invoice_number))
            db.session.commit()
            flash("success|" + t["unit.saved"])
            back = request.form.get("back") or url_for("stock.index")
            if not back.startswith("/") or back.startswith("//"):
                back = url_for("stock.index")
            return modal_ok() if is_modal_request() else redirect(back)
    return form_response("unit_form.html", "_unit_form.html", row=unit, error=error,
                         back=request.args.get("back") or "")
