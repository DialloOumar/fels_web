"""Panier — the seller's cart, and the proformas printed from it.

A vehicle is added to the cart from wherever it is seen: the Vehicles list,
its own page, a purchase. The cart is kept on the server, one per user, so
it follows the seller across pages, browsers and devices. Its page is the
sale form, filled with what is in the cart. There is nothing to save: what
is put in stays -- and a price typed is remembered with its vehicle -- until
the cart is emptied or becomes a sale (a pre-order or a sale). Printing a
proforma leaves it as it is.

A proforma (DEV-YYYY-NNN) is copied when it is issued, like an invoice, and
reserves nothing: the vehicles stay free until a sale names them. It can be
put back in the cart later, for whatever of it is still free, or deleted.
A deleted proforma's number is never given again: two papers in customers'
hands never carry the same one.
"""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from sqlalchemy.exc import IntegrityError

from app import (_safe_back, get_setting, get_t, is_modal_request, log_action, money_text, parse_date,
                 parse_money, set_setting)
from models import CURRENCIES, CartItem, Customer, Quote, Sale, Unit, db, next_number

cart_bp = Blueprint("cart", __name__, url_prefix="/panier")


# ── What the templates ask ───────────────────────────────────────────────────


def cart_ids():
    """The ids of the vehicles in the current user's cart, read once per request."""
    if not current_user.is_authenticated:
        return set()
    if "_cart_ids" not in g:
        g._cart_ids = {uid for (uid,) in db.session.query(CartItem.unit_id).filter_by(user_id=current_user.id)}
    return g._cart_ids


@cart_bp.app_context_processor
def _inject_cart():
    return {"cart_ids": cart_ids}


def _items():
    return (CartItem.query.filter_by(user_id=current_user.id)
            .order_by(CartItem.added_at, CartItem.id).all())


# ── Adding and taking out ────────────────────────────────────────────────────


def _answer(message, ok=True):
    """A click from a page answers in JSON (the page updates itself), a plain
    form post goes back where it came from."""
    if is_modal_request():
        return jsonify(ok=ok, message=message, count=len(cart_ids()), url=url_for("cart.index"))
    flash(("success|" if ok else "error|") + message)
    return redirect(_safe_back(request.form.get("back") or request.referrer) or url_for("cart.index"))


@cart_bp.route("/ajouter/<int:uid>", methods=["POST"])
@login_required
def add(uid):
    t = get_t()
    unit = db.session.get(Unit, uid) or abort(404)
    if unit.status != "available":
        return _answer(t["cart.error.taken"] % {"ref": unit.ref}, ok=False)
    if unit.id not in cart_ids():
        db.session.add(CartItem(user_id=current_user.id, unit_id=unit.id))
        db.session.commit()
        g.pop("_cart_ids", None)
    return _answer(t["cart.added"] % {"ref": unit.ref})


@cart_bp.route("/prix/<int:uid>", methods=["POST"])
@login_required
def price(uid):
    """A price typed on the cart page, remembered with its vehicle as it is
    typed: the cart is always as it was left."""
    raw = (request.form.get("price") or "").strip()
    value = parse_money(raw) if raw else None
    CartItem.query.filter_by(user_id=current_user.id, unit_id=uid).update(
        {"price": value if value is not None and value >= 0 else None})
    db.session.commit()
    return jsonify(ok=True)


@cart_bp.route("/retirer/<int:uid>", methods=["POST"])
@login_required
def remove(uid):
    CartItem.query.filter_by(user_id=current_user.id, unit_id=uid).delete()
    db.session.commit()
    g.pop("_cart_ids", None)
    unit = db.session.get(Unit, uid)
    return _answer(get_t()["cart.removed"] % {"ref": unit.ref if unit else uid})


# ── The cart page ────────────────────────────────────────────────────────────


def _sync(units_prices):
    """Make the cart what the page sent: these vehicles, these prices. A
    vehicle no longer free is left out."""
    keep = {}
    for uid, raw in units_prices:
        if not uid.isdigit():
            continue
        price = parse_money(raw) if raw.strip() else None
        keep[int(uid)] = price if price is not None and price >= 0 else None
    for item in _items():
        if item.unit_id not in keep:
            db.session.delete(item)
        else:
            item.price = keep.pop(item.unit_id)
    for uid, price in keep.items():
        unit = db.session.get(Unit, uid)
        if unit is not None and unit.status == "available":
            db.session.add(CartItem(user_id=current_user.id, unit_id=uid, price=price))
    db.session.commit()
    g.pop("_cart_ids", None)


def _posted_units():
    f = request.form
    units, prices = f.getlist("line_unit"), f.getlist("line_price")
    return [(u, prices[i] if i < len(prices) else "") for i, u in enumerate(units) if u]


@cart_bp.route("/", methods=["GET", "POST"])
@login_required
def index():
    from blueprints.sales import FormError, _form_context, _save
    t = get_t()
    error = None
    if request.method == "POST":
        action = request.form.get("action") or ""
        if action == "clear":
            CartItem.query.filter_by(user_id=current_user.id).delete()
            db.session.commit()
            flash("success|" + t["cart.cleared"])
            return redirect(url_for("cart.index"))
        _sync(_posted_units())
        try:
            if action == "quote":
                quote = _issue_quote()
                return redirect(url_for("cart.quote_print", qid=quote.id))
            if action in ("preorder", "confirmed"):
                sale = Sale()
                _save(sale, status=action)
                sold = {l.unit_id for l in sale.lines if l.unit_id}
                CartItem.query.filter(CartItem.user_id == current_user.id, CartItem.unit_id.in_(sold or {0})).delete(
                    synchronize_session=False)
                db.session.commit()
                flash("success|" + t["sale.saved"] % {"number": sale.number})
                return redirect(url_for("sales.detail", sid=sale.id))
            abort(400)
        except FormError as e:
            db.session.rollback()
            error = str(e)
    # What the cart holds; a vehicle sold since it was added is taken out.
    items, gone = [], []
    for item in _items():
        if item.unit.status == "available":
            items.append(item)
        else:
            gone.append(item.unit.ref)
            db.session.delete(item)
    if gone:
        db.session.commit()
        g.pop("_cart_ids", None)
        flash("warning|" + t["cart.gone"] % {"refs": ", ".join(gone)})
    from app import app
    money_field = app.jinja_env.filters["money_field"]
    cart_lines = [{"product_id": i.unit.product_id, "unit_id": i.unit_id,
                   "price": money_field(i.price) if i.price is not None else "", "description": ""} for i in items]
    return render_template("sale_form.html", row=None, error=error, preset={"customer": request.args.get("customer", type=int)},
                           preset_units=[], cart_mode=True, cart_lines=cart_lines, **_form_context(Sale()))


# ── Proformas ────────────────────────────────────────────────────────────────


def _issue_quote():
    """Check the cart page's form as a sale would be checked, and issue the
    proforma: customer, lines and terms copied as they are today."""
    from blueprints.sales import FormError, _parse_lines
    from blueprints.stock import road_label
    t = get_t()
    f = request.form
    customer = db.session.get(Customer, f.get("customer_id", type=int) or 0)
    if customer is None or not customer.is_active:
        raise FormError(t["sale.error.customer"])
    issued_on = parse_date(f.get("sale_date"))
    if issued_on is None:
        raise FormError(t["form.error.date"])
    currency = f.get("currency")
    if currency not in CURRENCIES:
        raise FormError(t["form.error.currency"])
    rows = _parse_lines(Sale())
    lines = []
    for r in rows:
        u = r["unit"]
        if u is None:
            where = t["quote.on_order"]
        else:
            p = u.purchase_line.purchase
            where = ("%s %s" % (t["stage.eta"], p.eta_date.strftime("%d/%m/%Y"))
                     if p.stage == "eta" and p.eta_date else road_label(t, u))
        lines.append({"unit_id": u.id if u else None, "product_id": r["product"].id,
                      "ref": u.ref if u else None, "product": r["product"].name, "description": r["description"],
                      "chassis": u.chassis_number if u else None, "where": where, "price": str(r["price"])})
    try:
        days = int(get_setting("proforma_valid_days", "15") or 15)
    except ValueError:
        days = 15
    try:
        pct = Decimal(get_setting("proforma_deposit_pct", "30") or 0)
    except InvalidOperation:
        pct = Decimal(0)
    quote = Quote(customer_id=customer.id, issued_on=issued_on, valid_until=issued_on + timedelta(days=days),
                  currency=currency, total=sum((r["price"] for r in rows), Decimal(0)),
                  deposit_pct=pct if pct > 0 else None,
                  customer_name=customer.name, customer_address=customer.address, customer_phone=customer.phone,
                  lines=lines, terms=(get_setting("proforma_terms", "") or "").strip() or None,
                  note=(f.get("note") or "").strip() or None, created_by=current_user.id)
    db.session.add(quote)
    _number_quote(quote, issued_on.year)
    log_action("CREATE", "quote", quote.id, "%s · %s · %s" % (quote.number, quote.customer_name,
                                                             money_text(quote.total, quote.currency)))
    db.session.commit()
    return quote


def _number_quote(quote, year):
    """Give the proforma the next number of its year and commit. The highest
    number ever given is kept in the settings table, so deleting the last
    proforma does not hand its number to the next one."""
    prefix, key = "DEV-%d-" % year, "quote_seq_%d" % year
    for _ in range(5):
        # Read without flushing: the proforma has no number yet.
        with db.session.no_autoflush:
            in_use = int(next_number(Quote.number, prefix)[len(prefix):])
            try:
                given = int(get_setting(key, "0") or 0)
            except ValueError:
                given = 0
        n = max(in_use, given + 1)
        quote.number = "%s%03d" % (prefix, n)
        set_setting(key, str(n))
        try:
            db.session.commit()
            return
        except IntegrityError:
            db.session.rollback()
            db.session.add(quote)
    raise RuntimeError("could not allocate a number in %s" % prefix)


@cart_bp.route("/proforma/<int:qid>/supprimer", methods=["POST"])
@login_required
def quote_delete(qid):
    """Delete a proforma for good. The activity log keeps what it was; its
    number is not given again."""
    quote = db.session.get(Quote, qid) or abort(404)
    log_action("DELETE", "quote", quote.id, "%s · %s · %s · %d ligne(s)" % (
        quote.number, quote.customer_name, money_text(quote.total, quote.currency), len(quote.lines)))
    number = quote.number
    db.session.delete(quote)
    db.session.commit()
    flash("success|" + get_t()["quote.deleted"] % {"number": number})
    return redirect(url_for("cart.quotes"))


@cart_bp.route("/proforma/<int:qid>")
@login_required
def quote_print(qid):
    quote = db.session.get(Quote, qid) or abort(404)
    company = {k: get_setting("company_" + k, "") for k in ("name", "phone", "email", "address", "nif", "rccm")}
    from models import Product
    ids = {l.get("product_id") for l in quote.lines if l.get("product_id")}
    covers = {p.id: p.cover for p in Product.query.filter(Product.id.in_(ids or {0}))}
    return render_template("quote_print.html", q=quote, company=company, covers=covers,
                           footer=get_setting("invoice_footer", "") or get_setting("receipt_footer", ""))


@cart_bp.route("/proformas")
@login_required
def quotes():
    from app import ListPage
    from blueprints.sales import _tab_counts
    rows = Quote.query.order_by(Quote.issued_on.desc(), Quote.id.desc()).all()
    pagination = ListPage(rows)
    return render_template("quotes.html", rows=pagination.items, pagination=pagination,
                           tab_counts=_tab_counts(), today=date.today())


@cart_bp.route("/proforma/<int:qid>/reprendre", methods=["POST"])
@login_required
def quote_reload(qid):
    """Put a proforma's vehicles back in the cart, at its prices: the
    customer said yes. What has been sold since stays out."""
    quote = db.session.get(Quote, qid) or abort(404)
    t = get_t()
    mine = cart_ids()
    added, gone = 0, []
    for line in quote.lines:
        uid = line.get("unit_id")
        if not uid:
            continue
        unit = db.session.get(Unit, uid)
        if unit is None or unit.status != "available":
            gone.append(line.get("ref") or "?")
            continue
        price = Decimal(line["price"]) if line.get("price") else None
        if uid in mine:
            CartItem.query.filter_by(user_id=current_user.id, unit_id=uid).update({"price": price})
        else:
            db.session.add(CartItem(user_id=current_user.id, unit_id=uid, price=price))
        added += 1
    db.session.commit()
    flash("success|" + t["cart.reloaded"] % {"n": added, "number": quote.number})
    if gone:
        flash("warning|" + t["cart.gone"] % {"refs": ", ".join(gone)})
    return redirect(url_for("cart.index", customer=quote.customer_id))
