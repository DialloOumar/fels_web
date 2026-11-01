"""Achats — supplier invoices, the road they travel, and what they cost.

A purchase is one supplier invoice: so many of each model at a price, landing
at a port. Each line's quantity becomes as many vehicles (Unit rows), so a
vehicle can be followed, given its chassis number and sold on its own.

The pipeline: ordered → shipped → at the port → in customs → cleared → in
stock. Each stage keeps the date it was reached, so the time spent at the port
or in customs can be read off. Moving back to an earlier stage (a correction)
clears the dates of the stages after it.

Extra costs (customs duty, port fees, transit, transport) are recorded on the
purchase, each in its own currency and rate, and spread over its vehicles in
proportion to price to give each its landed cost.
"""
import io
import os

from flask import (Blueprint, abort, flash, redirect, render_template, request,
                   send_file, url_for)
from flask_login import current_user, login_required

from exports import wants_xlsx, xlsx_response

import documents_store
from analytics import model_stats
from app import (ListPage, current_rate, form_response, fr, get_t,
                 is_modal_request, log_action, modal_ok, money_text, paginate,
                 parse_date, parse_money, parse_rate, rate_field_value)
from models import (COST_KINDS, CURRENCIES, DOCUMENT_KINDS, PURCHASE_STAGES,
                    STAGE_DATE_FIELD, Port, Product, Purchase, PurchaseCost,
                    PurchaseDocument, PurchaseLine, Supplier, Unit,
                    assign_unit_refs, db)

purchases_bp = Blueprint("purchases", __name__, url_prefix="/achats")

MAX_QTY = 500
PAY_STATUSES = ("unpaid", "partial", "paid")


def _get_or_404(pid):
    p = db.session.get(Purchase, pid)
    if p is None:
        abort(404)
    return p


def next_stage(stage):
    i = PURCHASE_STAGES.index(stage)
    return PURCHASE_STAGES[i + 1] if i + 1 < len(PURCHASE_STAGES) else None


def days_in_stage(purchase, today):
    d = purchase.stage_date(purchase.stage)
    return (today - d).days if d else None


# ── List ─────────────────────────────────────────────────────────────────────


@purchases_bp.route("/")
@login_required
def index():
    search = (request.args.get("q") or "").strip()
    supplier_id = request.args.get("supplier", type=int)
    stage = request.args.get("stage") or ""
    port_id = request.args.get("port", type=int)
    pay = request.args.get("pay") or ""
    q = Purchase.query
    if supplier_id:
        q = q.filter(Purchase.supplier_id == supplier_id)
    if stage in PURCHASE_STAGES:
        q = q.filter(Purchase.stage == stage)
    else:
        stage = ""
    if port_id:
        q = q.filter(Purchase.port_id == port_id)
    if search:
        like = "%" + search + "%"
        q = q.outerjoin(PurchaseLine).outerjoin(Product).filter(db.or_(
            Purchase.invoice_number.ilike(like), Purchase.note.ilike(like),
            Product.name.ilike(like), Product.code.ilike(like))).distinct()
    q = q.order_by(Purchase.invoice_date.desc(), Purchase.id.desc())
    if pay not in PAY_STATUSES:
        pay = ""
    if wants_xlsx(request):
        rows = [p for p in q.all() if not pay or p.pay_status == pay]
        return _export(rows)
    if pay:
        # Paid / part paid is computed from the payments: filtered here.
        pagination = ListPage([p for p in q.all() if p.pay_status == pay])
    else:
        pagination = paginate(q)
    t = get_t()
    opts = {
        "suppliers": [(s.id, s.name) for s in Supplier.query.order_by(Supplier.name)],
        "stages": [(s, t["stage." + s]) for s in PURCHASE_STAGES],
        "ports": [(p.id, p.name) for p in Port.query.order_by(Port.sort_order, Port.name)],
        "pay": [(s, t["pay." + s]) for s in PAY_STATUSES],
    }
    return render_template(
        "purchases.html", rows=pagination.items, pagination=pagination, search=search,
        supplier_id=supplier_id, stage=stage, port_id=port_id, pay=pay, opts=opts)


def _export(rows):
    t = get_t()
    headers = [t["purchase.invoice_number"], t["purchase.supplier"], t["purchase.invoice_date"], t["purchase.port"],
               t["purchase.stage"], t["purchase.vehicles"], t["purchase.currency"], t["purchase.goods_total"],
               t["purchase.costs_total"], t["purchase.paid_supplier"], t["purchase.balance"], t["purchase.payment"],
               t["field.note"]]
    data = [[p.invoice_number, p.supplier.name, p.invoice_date, p.port.name if p.port else None,
             t["stage." + p.stage], p.unit_count, p.currency, p.goods_total, p.costs_total(),
             p.paid_amount, p.balance, t["pay." + p.pay_status], p.note] for p in rows]
    return xlsx_response("achats", t["nav.purchases"], headers, data)


# ── Pipeline board ───────────────────────────────────────────────────────────


@purchases_bp.route("/pipeline")
@login_required
def pipeline():
    """Purchases on their way, one column per stage. What has reached the
    stock is counted, not shown: the board is about what is still moving."""
    from datetime import date
    moving = (Purchase.query.filter(Purchase.stage != "in_stock")
              .order_by(Purchase.invoice_date).all())
    columns = {s: [] for s in PURCHASE_STAGES if s != "in_stock"}
    for p in moving:
        columns[p.stage].append(p)
    in_stock = Purchase.query.filter_by(stage="in_stock").count()
    return render_template("pipeline.html", columns=columns, in_stock=in_stock,
                           today=date.today(), days_in_stage=days_in_stage,
                           next_stage=next_stage)


# ── Create / edit ────────────────────────────────────────────────────────────


class FormError(Exception):
    pass


def _parse_lines():
    """The lines editor, row by row. Empty rows are skipped. Returns a list
    of dicts, or raises FormError."""
    t = get_t()
    f = request.form
    ids, prods = f.getlist("line_id"), f.getlist("line_product")
    qtys, prices, descs = f.getlist("line_qty"), f.getlist("line_price"), f.getlist("line_desc")
    rows = []
    for i in range(len(prods)):
        pid = (prods[i] or "").strip()
        raw_q = (qtys[i] if i < len(qtys) else "").strip()
        raw_p = (prices[i] if i < len(prices) else "").strip()
        desc = (descs[i] if i < len(descs) else "").strip()
        if not pid and not raw_p and not desc:
            continue
        product = db.session.get(Product, int(pid)) if pid.isdigit() else None
        if product is None:
            raise FormError(t["purchase.error.line_product"] % {"n": i + 1})
        try:
            qty = int(raw_q)
        except ValueError:
            qty = 0
        if not 1 <= qty <= MAX_QTY:
            raise FormError(t["purchase.error.line_qty"] % {"n": i + 1, "max": MAX_QTY})
        price = parse_money(raw_p) if raw_p else None
        if price is None or price < 0:
            raise FormError(t["purchase.error.line_price"] % {"n": i + 1})
        lid = ids[i] if i < len(ids) else ""
        rows.append({"id": int(lid) if lid.isdigit() else None, "product": product,
                     "quantity": qty, "unit_price": price, "description": desc[:255] or None})
    if not rows:
        raise FormError(t["purchase.error.no_lines"])
    return rows


def _sync_units(line):
    """Make the line's vehicles match its quantity and model.

    New vehicles are added; surplus ones removed, those without a chassis
    number first. A vehicle on a live sale is never removed nor changed to
    another model -- the form is refused instead."""
    t = get_t()
    units = list(line.units)
    sold = [u for u in units if u.active_sale_line is not None]
    if sold and any(u.product_id != line.product_id for u in sold):
        raise FormError(t["purchase.error.sold_model"])
    for u in units:
        if u.product_id != line.product_id:
            u.product, u.ref, u.seq = line.product, None, None    # renumbered under its new model
    missing = line.quantity - len(units)
    if missing > 0:
        for _ in range(missing):
            line.units.append(Unit(product=line.product))
    elif missing < 0:
        free = [u for u in reversed(units) if u.active_sale_line is None]
        free.sort(key=lambda u: u.chassis_number is not None)
        if len(free) < -missing:
            raise FormError(t["purchase.error.sold_qty"])
        for u in free[:-missing]:
            line.units.remove(u)


def _save(purchase):
    """Copy the form onto `purchase` (new or existing). Raises FormError;
    the caller rolls back."""
    t = get_t()
    f = request.form
    supplier = db.session.get(Supplier, f.get("supplier_id", type=int) or 0)
    if supplier is None or (not supplier.is_active and supplier.id != purchase.supplier_id):
        raise FormError(t["purchase.error.supplier"])
    number = (f.get("invoice_number") or "").strip()
    if not number:
        raise FormError(t["purchase.error.number"])
    invoice_date = parse_date(f.get("invoice_date"))
    if invoice_date is None:
        raise FormError(t["form.error.date"])
    currency = f.get("currency")
    if currency not in CURRENCIES:
        raise FormError(t["form.error.currency"])
    port = None
    if f.get("port_id"):
        port = db.session.get(Port, f.get("port_id", type=int) or 0)
        if port is None:
            raise FormError(t["purchase.error.port"])
    clash = Purchase.query.filter(Purchase.supplier_id == supplier.id,
                                  db.func.lower(Purchase.invoice_number) == number.lower())
    if purchase.id:
        clash = clash.filter(Purchase.id != purchase.id)
    if clash.first():
        raise FormError(t["purchase.error.number_exists"])
    rows = _parse_lines()

    is_new = purchase.id is None
    purchase.supplier = supplier
    purchase.invoice_number = number[:60]
    purchase.invoice_date = invoice_date
    purchase.currency = currency
    purchase.port = port
    purchase.note = (f.get("note") or "").strip() or None
    if purchase.id is None:
        purchase.stage = "ordered"
        purchase.ordered_on = invoice_date
        purchase.created_by = current_user.id
        db.session.add(purchase)

    existing = {l.id: l for l in purchase.lines}
    kept = set()
    for r in rows:
        line = existing.get(r["id"]) if r["id"] else None
        if line is None:
            line = PurchaseLine()
            purchase.lines.append(line)
        else:
            kept.add(line.id)
        line.product = r["product"]
        line.quantity = r["quantity"]
        line.unit_price = r["unit_price"]
        line.description = r["description"]
    for lid, line in existing.items():
        if lid not in kept:
            if any(u.active_sale_line is not None for u in line.units):
                raise FormError(t["purchase.error.sold_line"])
            purchase.lines.remove(line)
    for line in purchase.lines:
        _sync_units(line)
    assign_unit_refs()
    log_action("CREATE" if is_new else "UPDATE", "purchase", purchase.id,
               "%s · %s · %s" % (purchase.invoice_number, supplier.name, money_text(purchase.goods_total, purchase.currency)))
    db.session.commit()


def _form_context(purchase):
    suppliers = Supplier.query.filter(db.or_(Supplier.is_active.is_(True),
                                             Supplier.id == (purchase.supplier_id or 0))
                                      ).order_by(Supplier.name).all()
    products = Product.query.filter(db.or_(
        Product.is_active.is_(True),
        Product.id.in_([l.product_id for l in purchase.lines] or [0]))).order_by(Product.category, Product.name).all()
    ports = Port.query.filter(db.or_(Port.is_active.is_(True), Port.id == (purchase.port_id or 0))
                              ).order_by(Port.sort_order, Port.name).all()
    stats = model_stats([p.id for p in products], exclude_purchase=purchase.id)
    model_figures = {p.id: {k: stats[p.id][k] for k in ("orders", "incoming", "in_stock", "reserved", "free")}
                     for p in products}
    return {"suppliers": suppliers, "products": products, "ports": ports, "model_figures": model_figures}


def _already_on_the_way(purchase):
    """The models of a new purchase that other purchases are already
    bringing, as "Shacman 430 HP (3)": what to look at twice before ordering
    more."""
    ids = {l.product_id for l in purchase.lines}
    stats = model_stats(ids, exclude_purchase=purchase.id)
    return ["%s (%d)" % (l.product.name, stats[l.product_id]["incoming"])
            for l in sorted({l.product_id: l for l in purchase.lines}.values(), key=lambda l: l.product.name)
            if stats[l.product_id]["incoming"]]


@purchases_bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    purchase, error = Purchase(), None
    if request.method == "POST":
        try:
            _save(purchase)
            t = get_t()
            flash("success|" + t["purchase.saved"])
            on_the_way = _already_on_the_way(purchase)
            if on_the_way:
                flash("warning|" + t["purchase.warn.on_the_way"] % {"models": ", ".join(on_the_way)})
            return redirect(url_for("purchases.detail", pid=purchase.id))
        except FormError as e:
            db.session.rollback()
            error = str(e)
        purchase = Purchase()
    preset = request.args.get("supplier", type=int)
    return render_template("purchase_form.html", row=None, error=error, preset_supplier=preset,
                           **_form_context(purchase))


@purchases_bp.route("/<int:pid>/edit", methods=["GET", "POST"])
@login_required
def edit(pid):
    purchase, error = _get_or_404(pid), None
    if request.method == "POST":
        try:
            _save(purchase)
            flash("success|" + get_t()["purchase.saved"])
            return redirect(url_for("purchases.detail", pid=purchase.id))
        except FormError as e:
            db.session.rollback()
            error = str(e)
            purchase = _get_or_404(pid)
    return render_template("purchase_form.html", row=purchase, error=error, preset_supplier=None,
                           **_form_context(purchase))


@purchases_bp.route("/<int:pid>")
@login_required
def detail(pid):
    from datetime import date
    purchase = _get_or_404(pid)
    shares = purchase.extra_cost_per_unit()
    units = [u for l in purchase.lines for u in l.units]
    payments = sorted(list(purchase.payments) + [p for c in purchase.costs for p in c.payments],
                      key=lambda p: (p.paid_on, p.id))
    return render_template("purchase_detail.html", row=purchase, shares=shares, units=units,
                           payments=payments, stages=PURCHASE_STAGES, next_stage=next_stage(purchase.stage),
                           days=days_in_stage(purchase, date.today()))


@purchases_bp.route("/<int:pid>/delete", methods=["POST"])
@login_required
def delete(pid):
    """Only a purchase nothing hangs on: no payment (even voided -- it was
    money once) and no vehicle on a sale."""
    purchase = _get_or_404(pid)
    t = get_t()
    has_payments = purchase.payments or any(c.payments for c in purchase.costs)
    has_sales = any(u.sale_lines for l in purchase.lines for u in l.units)
    if has_payments or has_sales:
        flash("error|" + t["purchase.error.delete"])
        return redirect(url_for("purchases.detail", pid=pid))
    log_action("DELETE", "purchase", purchase.id, "%s · %s" % (purchase.invoice_number, purchase.supplier.name))
    files = [d.storage_key for d in purchase.documents]
    db.session.delete(purchase)
    db.session.commit()
    for key in files:
        documents_store.remove(key)
    flash("success|" + t["delete.done"])
    return redirect(url_for("purchases.index"))


# ── Stage ────────────────────────────────────────────────────────────────────


@purchases_bp.route("/<int:pid>/etape", methods=["GET", "POST"])
@login_required
def stage(pid):
    """Move a purchase to a stage, on a date. Forward is the usual move;
    an earlier stage is a correction and clears the later dates."""
    from datetime import date
    purchase = _get_or_404(pid)
    t = get_t()
    error = None
    if request.method == "POST":
        target = request.form.get("stage")
        when = parse_date(request.form.get("date"))
        if target not in PURCHASE_STAGES:
            error = t["purchase.error.stage"]
        elif when is None:
            error = t["form.error.date"]
        else:
            idx = PURCHASE_STAGES.index(target)
            setattr(purchase, STAGE_DATE_FIELD[target], when)
            for later in PURCHASE_STAGES[idx + 1:]:
                setattr(purchase, STAGE_DATE_FIELD[later], None)
            log_action("STAGE", "purchase", purchase.id, "%s · %s → %s · %s" % (
                purchase.invoice_number, fr("stage." + purchase.stage), fr("stage." + target), when.strftime("%d/%m/%Y")))
            purchase.stage = target
            db.session.commit()
            flash("success|" + t["purchase.stage_saved"] % {"stage": t["stage." + target]})
            back = request.form.get("back") or url_for("purchases.detail", pid=pid)
            if not back.startswith("/") or back.startswith("//"):
                back = url_for("purchases.detail", pid=pid)
            return modal_ok() if is_modal_request() else redirect(back)
    proposed = request.args.get("to") or next_stage(purchase.stage) or purchase.stage
    if proposed not in PURCHASE_STAGES:
        proposed = purchase.stage
    current_date = purchase.stage_date(proposed)
    return form_response("stage_form.html", "_stage_form.html", row=purchase, error=error,
                         stages=PURCHASE_STAGES, proposed=proposed,
                         proposed_date=(current_date or date.today()).isoformat(),
                         back=request.args.get("back") or "")


# ── Extra costs ──────────────────────────────────────────────────────────────


def _save_cost(cost, purchase):
    t = get_t()
    f = request.form
    kind = f.get("kind") or "other"
    amount = parse_money(f.get("amount"))
    currency = f.get("currency")
    raw_rate = (f.get("rate") or "").strip()
    rate = parse_rate(raw_rate)
    incurred_on = parse_date(f.get("incurred_on"))
    if kind not in COST_KINDS:
        raise FormError(t["cost.error.kind"])
    if amount is None or amount <= 0:
        raise FormError(t["form.error.amount"])
    if currency not in CURRENCIES:
        raise FormError(t["form.error.currency"])
    if raw_rate and rate is None:
        raise FormError(t["form.error.rate"])
    if currency != purchase.currency and rate is None:
        raise FormError(t["cost.error.rate_needed"] % {"cur": purchase.currency})
    if incurred_on is None:
        raise FormError(t["form.error.date"])
    cost.kind, cost.amount, cost.currency, cost.rate, cost.incurred_on = kind, amount, currency, rate, incurred_on
    cost.description = (f.get("description") or "").strip()[:255] or None
    cost.payee = (f.get("payee") or "").strip()[:120] or None
    cost.reference = (f.get("reference") or "").strip()[:60] or None
    is_new = cost.id is None
    if is_new:
        cost.purchase = purchase
        db.session.add(cost)
        db.session.flush()
    log_action("CREATE" if is_new else "UPDATE", "purchase_cost", cost.id, "%s · %s · %s" % (
        purchase.invoice_number, fr("cost.kind." + kind), money_text(amount, currency)))
    db.session.commit()


def _cost_form(purchase, cost):
    from datetime import date
    t = get_t()
    error = None
    if request.method == "POST":
        try:
            _save_cost(cost, purchase)
            flash("success|" + t["cost.saved"])
            return modal_ok() if is_modal_request() else redirect(url_for("purchases.detail", pid=purchase.id))
        except FormError as e:
            db.session.rollback()
            error = str(e)
    return form_response("cost_form.html", "_cost_form.html", purchase=purchase,
                         row=cost if cost.id else None, error=error, kinds=COST_KINDS,
                         default_rate=rate_field_value(current_rate()), today=date.today().isoformat())


@purchases_bp.route("/<int:pid>/frais/new", methods=["GET", "POST"])
@login_required
def cost_new(pid):
    return _cost_form(_get_or_404(pid), PurchaseCost())


@purchases_bp.route("/frais/<int:cid>/edit", methods=["GET", "POST"])
@login_required
def cost_edit(cid):
    cost = db.session.get(PurchaseCost, cid) or abort(404)
    return _cost_form(cost.purchase, cost)


@purchases_bp.route("/frais/<int:cid>/delete", methods=["POST"])
@login_required
def cost_delete(cid):
    cost = db.session.get(PurchaseCost, cid) or abort(404)
    t = get_t()
    pid = cost.purchase_id
    if cost.payments:
        flash("error|" + t["cost.error.has_payments"])
    else:
        log_action("DELETE", "purchase_cost", cost.id, "%s · %s · %s" % (
            cost.purchase.invoice_number, fr("cost.kind." + cost.kind), money_text(cost.amount, cost.currency)))
        db.session.delete(cost)
        db.session.commit()
        flash("success|" + t["delete.done"])
    return redirect(url_for("purchases.detail", pid=pid))


# ── Documents ────────────────────────────────────────────────────────────────


def _save_document(purchase):
    """Check the upload form, keep the file, then record it. Returns an error
    message, or None."""
    t = get_t()
    f = request.form
    kind = f.get("kind") or ""
    title = (f.get("title") or "").strip()[:120] or None
    number = (f.get("number") or "").strip()[:60] or None
    raw_date = (f.get("doc_date") or "").strip()
    doc_date = parse_date(raw_date) if raw_date else None
    upload = request.files.get("file")
    if kind not in DOCUMENT_KINDS:
        return t["doc.error.kind"]
    if kind == "other" and not title:
        return t["doc.error.title"]
    if raw_date and doc_date is None:
        return t["form.error.date"]
    if not upload or not upload.filename:
        return t["doc.error.file"]
    name = os.path.basename(upload.filename.replace("\\", "/"))[:200]
    ext = os.path.splitext(name)[1].lower()
    if ext not in documents_store.ALLOWED:
        return t["doc.error.type"]
    data = upload.read(documents_store.MAX_BYTES + 1)
    if not data:
        return t["doc.error.file"]
    if len(data) > documents_store.MAX_BYTES:
        return t["doc.error.size"] % {"mb": documents_store.MAX_BYTES // (1024 * 1024)}
    content_type = documents_store.ALLOWED[ext]
    key = documents_store.new_key(purchase.id, ext)
    if not documents_store.save(key, data, content_type):
        return t["doc.error.storage"]
    doc = PurchaseDocument(purchase=purchase, kind=kind, title=title, number=number, doc_date=doc_date,
                           filename=name, content_type=content_type, size=len(data), storage_key=key,
                           uploaded_by=current_user.id)
    db.session.add(doc)
    try:
        db.session.flush()
        log_action("CREATE", "document", doc.id, "%s · %s · %s" % (
            purchase.invoice_number, title or fr("doc.kind." + kind), name))
        db.session.commit()
    except Exception:
        db.session.rollback()
        documents_store.remove(key)          # no row, no file
        raise
    return None


@purchases_bp.route("/<int:pid>/documents/new", methods=["GET", "POST"])
@login_required
def document_new(pid):
    from datetime import date
    purchase, error = _get_or_404(pid), None
    if request.method == "POST":
        error = _save_document(purchase)
        if error is None:
            flash("success|" + get_t()["doc.saved"])
            return modal_ok() if is_modal_request() else redirect(url_for("purchases.detail", pid=pid))
    return form_response("document_form.html", "_document_form.html", purchase=purchase, error=error,
                         kinds=DOCUMENT_KINDS, today=date.today().isoformat(),
                         accept=",".join(documents_store.ALLOWED),
                         max_mb=documents_store.MAX_BYTES // (1024 * 1024))


@purchases_bp.route("/documents/<int:did>")
@login_required
def document_file(did):
    """The file itself: a PDF or a photo opens in the browser, anything else
    downloads (and ?download=1 downloads anything)."""
    doc = db.session.get(PurchaseDocument, did) or abort(404)
    data = documents_store.load(doc.storage_key)
    if data is None:
        flash("error|" + get_t()["doc.error.missing"])
        return redirect(url_for("purchases.detail", pid=doc.purchase_id))
    inline = doc.content_type in documents_store.SHOWN_INLINE and not request.args.get("download")
    resp = send_file(io.BytesIO(data), mimetype=doc.content_type, as_attachment=not inline,
                     download_name=doc.filename, max_age=0)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


@purchases_bp.route("/documents/<int:did>/delete", methods=["POST"])
@login_required
def document_delete(did):
    doc = db.session.get(PurchaseDocument, did) or abort(404)
    pid, key = doc.purchase_id, doc.storage_key
    log_action("DELETE", "document", doc.id, "%s · %s · %s" % (
        doc.purchase.invoice_number, doc.title or fr("doc.kind." + doc.kind), doc.filename))
    db.session.delete(doc)
    db.session.commit()
    documents_store.remove(key)
    flash("success|" + get_t()["delete.done"])
    return redirect(url_for("purchases.detail", pid=pid))

