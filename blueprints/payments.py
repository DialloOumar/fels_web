"""Paiements — money that moved, once.

This module records payments out: to a supplier against a purchase, or
against one of its extra costs (customs, port, transit...). Receipts from
customers are taken on the sale (blueprints/sales.py), numbered REC-YYYY-NNN,
and voided here like any payment.

Every payment keeps its own currency and the rate of its day, so a USD
purchase can be settled partly in GNF and its balance still reads right. A
payment entered by mistake is voided, never deleted: it stays in the history,
struck through, and stops counting.
"""
from decimal import Decimal

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app import (ListPage, current_rate, form_response, fr, get_t,
                 is_modal_request, log_action, modal_ok, money_text, parse_date,
                 parse_money, parse_rate, rate_field_value)
from exports import wants_xlsx, xlsx_response
from models import (CURRENCIES, PAYMENT_METHODS, Customer, Payment, Purchase,
                    PurchaseCost, Sale, Supplier, convert, db, utcnow)

payments_bp = Blueprint("payments", __name__, url_prefix="/paiements")


def _parse_payment(target_currency):
    """Read the payment form. Returns (values, error)."""
    t = get_t()
    f = request.form
    paid_on = parse_date(f.get("paid_on"))
    amount = parse_money(f.get("amount"))
    currency = f.get("currency") or target_currency
    raw_rate = (f.get("rate") or "").strip()
    rate = parse_rate(raw_rate)
    method = f.get("method") or "cash"
    if paid_on is None:
        return None, t["form.error.date"]
    if amount is None or amount <= 0:
        return None, t["form.error.amount"]
    if currency not in CURRENCIES:
        return None, t["form.error.currency"]
    if raw_rate and rate is None:
        return None, t["form.error.rate"]
    if currency != target_currency and rate is None:
        return None, t["payment.error.rate_needed"] % {"cur": target_currency}
    if method not in PAYMENT_METHODS:
        return None, t["payment.error.method"]
    return {
        "paid_on": paid_on, "amount": amount, "currency": currency, "rate": rate,
        "method": method,
        "reference": (f.get("reference") or "").strip() or None,
        "note": (f.get("note") or "").strip() or None,
    }, None


def _record(target, target_currency, balance, back_url, form_action, title):
    """Show and handle the form for a payment out against `target` (a
    Purchase or a PurchaseCost)."""
    t = get_t()
    error = None
    if request.method == "POST":
        values, error = _parse_payment(target_currency)
        if error is None:
            p = Payment(direction="out", created_by=current_user.id, **values)
            if isinstance(target, Purchase):
                p.purchase_id = target.id
            else:
                p.purchase_cost_id = target.id
            db.session.add(p)
            db.session.flush()
            log_action("CREATE", "payment", p.id, "%s · %s · %s" % (
                _payment_label(p), money_text(p.amount, p.currency), fr("method." + p.method)))
            db.session.commit()
            flash("success|" + t["payment.saved"])
            in_target = convert(values["amount"], values["currency"], target_currency, values["rate"])
            if in_target is not None and in_target > balance + Decimal("0.01"):
                flash("warning|" + t["payment.warn.over_balance"])
            return modal_ok() if is_modal_request() else redirect(back_url)
    return form_response("payment_form.html", "_payment_form.html", error=error,
                         target_currency=target_currency, balance=balance,
                         default_rate=rate_field_value(current_rate()),
                         methods=PAYMENT_METHODS, form_action=form_action,
                         back_url=back_url, title=title)


@payments_bp.route("/achat/<int:pid>/nouveau", methods=["GET", "POST"])
@login_required
def purchase_payment(pid):
    purchase = db.session.get(Purchase, pid) or abort(404)
    t = get_t()
    return _record(purchase, purchase.currency, purchase.balance,
                   url_for("purchases.detail", pid=pid),
                   url_for("payments.purchase_payment", pid=pid),
                   t["payment.new_supplier"])


@payments_bp.route("/frais/<int:cid>/nouveau", methods=["GET", "POST"])
@login_required
def cost_payment(cid):
    cost = db.session.get(PurchaseCost, cid) or abort(404)
    t = get_t()
    return _record(cost, cost.currency, cost.balance,
                   url_for("purchases.detail", pid=cost.purchase_id),
                   url_for("payments.cost_payment", cid=cid),
                   t["payment.new_cost"])


def _payment_label(p):
    """What a payment is for, as the audit log names it: the receipt and
    its sale, the supplier's invoice, or the invoice and the cost."""
    if p.sale_id:
        return "%s · %s" % (p.receipt_number, p.sale.number)
    if p.purchase_id:
        return "%s · %s" % (p.purchase.invoice_number, p.purchase.supplier.name)
    c = p.purchase_cost
    return "%s · %s" % (c.purchase.invoice_number, fr("cost.kind." + c.kind))


def _back_for(payment):
    if payment.sale_id:
        return url_for("sales.detail", sid=payment.sale_id)
    if payment.purchase_id:
        return url_for("purchases.detail", pid=payment.purchase_id)
    if payment.purchase_cost_id:
        return url_for("purchases.detail", pid=payment.purchase_cost.purchase_id)
    return url_for("dashboard")


@payments_bp.route("/<int:pay_id>/annuler", methods=["GET", "POST"])
@login_required
def void(pay_id):
    """Void a payment: it stays, struck through, and stops counting. A reason
    is required -- it is what someone reading the history later will need."""
    payment = db.session.get(Payment, pay_id) or abort(404)
    t = get_t()
    back = _back_for(payment)
    if payment.is_voided:
        flash("error|" + t["payment.already_voided"])
        return modal_ok() if is_modal_request() else redirect(back)
    error = None
    if request.method == "POST":
        reason = (request.form.get("reason") or "").strip()
        if not reason:
            error = t["payment.error.reason"]
        else:
            payment.voided_at = utcnow()
            payment.void_reason = reason[:255]
            log_action("VOID", "receipt" if payment.sale_id else "payment", payment.id, "%s · %s · %s" % (
                _payment_label(payment), money_text(payment.amount, payment.currency), payment.void_reason))
            db.session.commit()
            flash("success|" + t["payment.voided"])
            return modal_ok() if is_modal_request() else redirect(back)
    return form_response("payment_void.html", "_payment_void.html",
                         payment=payment, error=error, back_url=back)


# ── The ledger ───────────────────────────────────────────────────────────────


def counterparty(p):
    """Who the money came from or went to, and what it was for."""
    if p.sale is not None:
        return p.sale.customer.name, p.sale.number
    if p.purchase is not None:
        return p.purchase.supplier.name, p.purchase.invoice_number
    c = p.purchase_cost
    return (c.payee or c.purchase.supplier.name), c.purchase.invoice_number


@payments_bp.route("/")
@login_required
def index():
    """Every payment, in and out, newest first, with the totals of what the
    filters keep -- one per currency and direction, voided ones left out."""
    t = get_t()
    a = request.args
    direction = a.get("direction") if a.get("direction") in ("in", "out") else ""
    currency = a.get("currency") if a.get("currency") in CURRENCIES else ""
    method = a.get("method") if a.get("method") in PAYMENT_METHODS else ""
    voided = a.get("voided") if a.get("voided") in ("yes", "only") else ""
    date_from, date_to = parse_date(a.get("from")), parse_date(a.get("to"))
    search = (a.get("q") or "").strip()
    q = Payment.query
    if direction:
        q = q.filter(Payment.direction == direction)
    if currency:
        q = q.filter(Payment.currency == currency)
    if method:
        q = q.filter(Payment.method == method)
    if voided == "only":
        q = q.filter(Payment.voided_at.isnot(None))
    elif voided != "yes":
        q = q.filter(Payment.voided_at.is_(None))
    if date_from:
        q = q.filter(Payment.paid_on >= date_from)
    if date_to:
        q = q.filter(Payment.paid_on <= date_to)
    rows = q.order_by(Payment.paid_on.desc(), Payment.id.desc()).all()
    if search:
        needle = search.lower()
        rows = [p for p in rows if needle in " ".join(filter(None, [
            p.receipt_number, p.reference, p.note, *counterparty(p)])).lower()]
    totals = {}
    for p in rows:
        if p.is_voided:
            continue
        key = (p.direction, p.currency)
        totals[key] = totals.get(key, 0) + p.amount
    if wants_xlsx(request):
        headers = [t["payment.paid_on"], t["ledger.direction"], t["receipt.number"], t["ledger.party"],
                   t["payment.for"], t["payment.method"], t["field.reference"], t["purchase.currency"],
                   t["payment.amount"], t["field.rate"], t["ledger.status"], t["field.note"]]
        data = []
        for p in rows:
            party, doc = counterparty(p)
            data.append([p.paid_on, t["ledger.dir." + p.direction], p.receipt_number, party, doc,
                         t["method." + p.method], p.reference, p.currency,
                         (p.amount if p.direction == "in" else -p.amount), p.rate,
                         t["ledger.voided"] if p.is_voided else "", p.void_reason if p.is_voided else p.note])
        return xlsx_response("paiements", t["nav.payments"], headers, data)
    pagination = ListPage(rows)
    opts = {
        "direction": [("in", t["ledger.dir.in"]), ("out", t["ledger.dir.out"])],
        "currency": [(c, c) for c in CURRENCIES],
        "method": [(m, t["method." + m]) for m in PAYMENT_METHODS],
        "voided": [("yes", t["ledger.voided_include"]), ("only", t["ledger.voided_only"])],
    }
    return render_template("payments.html", rows=pagination.items, pagination=pagination, totals=totals,
                           direction=direction, currency=currency, method=method, voided=voided,
                           date_from=a.get("from") or "", date_to=a.get("to") or "", search=search,
                           opts=opts, counterparty=counterparty)
