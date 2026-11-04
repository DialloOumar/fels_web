"""Paiements — money that moved, once.

This module records payments out: to a supplier against a purchase, or
against one of its extra costs (customs, port, transit...). Receipts from
customers are taken on the sale (blueprints/sales.py), numbered REC-YYYY-NNN,
and voided here like any payment.

Every payment keeps its own currency and the rate of its day, so a USD
purchase can be settled partly in GNF and its balance still reads right. A
payment entered by mistake is voided, never deleted: it stays in the history,
struck through, and stops counting.

A payment just entered with a mistake in it can still be corrected or
deleted, for a short while: the correction window in the settings (24 hours
unless changed, 0 turns it off), counted from when it was entered. Past it,
voiding is the only way. Every correction and deletion is written to the
activity log with the values it replaced, so nothing goes without a trace.
"""
import io
from datetime import timedelta
from decimal import Decimal

from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required

from app import (ListPage, current_rate, form_response, fr, get_setting, get_t,
                 is_modal_request, log_action, modal_ok, money_text, parse_date,
                 parse_money, parse_rate, rate_field_value)
import documents_store
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


# ── The proof of a payment ──────────────────────────────────────────────────


def read_proof(required):
    """The proof sent with the payment form, checked. Returns (upload or
    None, error message or None)."""
    t = get_t()
    upload, err = documents_store.read_upload(request.files.get("proof"))
    if err == "file":
        return None, (t["payment.error.proof"] if required else None)
    if err == "type":
        return None, t["doc.error.type"]
    if err == "size":
        return None, t["doc.error.size"] % {"mb": documents_store.MAX_BYTES // (1024 * 1024)}
    return upload, None


def attach_proof(p, upload):
    """Keep the file and point the payment at it. False when the storage
    refused it. The previous proof, if any, is removed once replaced."""
    name, ext, data = upload
    key = documents_store.payment_key(p.id, ext)
    if not documents_store.save(key, data, documents_store.ALLOWED[ext]):
        return False
    old = p.proof_key
    p.proof_key, p.proof_filename, p.proof_type, p.proof_size = key, name, documents_store.ALLOWED[ext], len(data)
    if old:
        documents_store.remove(old)
    return True


def proof_form_context():
    return {"proof_accept": ",".join(documents_store.ALLOWED),
            "proof_max_mb": documents_store.MAX_BYTES // (1024 * 1024)}


def _record(target, target_currency, balance, back_url, form_action, title):
    """Show and handle the form for a payment out against `target` (a
    Purchase or a PurchaseCost)."""
    t = get_t()
    error = None
    if request.method == "POST":
        values, error = _parse_payment(target_currency)
        upload = None
        if error is None:
            upload, error = read_proof(required=True)
        if error is None:
            p = Payment(direction="out", created_by=current_user.id, **values)
            if isinstance(target, Purchase):
                p.purchase_id = target.id
            else:
                p.purchase_cost_id = target.id
            db.session.add(p)
            db.session.flush()
            if not attach_proof(p, upload):
                db.session.rollback()
                error = t["doc.error.storage"]
        if error is None:
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
                         back_url=back_url, title=title, proof_required=True, **proof_form_context())


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


# ── Correcting a payment, within the window ─────────────────────────────────


def correction_hours():
    """How long after it is entered a payment may still be corrected or
    deleted, in hours. 0: never."""
    try:
        return max(int(get_setting("payment_fix_hours", "24") or 0), 0)
    except (TypeError, ValueError):
        return 0


def correction_deadline(p):
    """Until when `p` may be corrected, or None when it may not be at all."""
    hours = correction_hours()
    if not hours or p.is_voided or p.created_at is None:
        return None
    return p.created_at + timedelta(hours=hours)


@payments_bp.app_template_global("can_correct")
def can_correct(p):
    """Whether the current user may still correct or delete `p`: not voided,
    inside the window, and allowed to enter that kind of payment -- a receipt
    with the sales, a payment out with paying suppliers."""
    from access import can
    deadline = correction_deadline(p)
    if deadline is None or utcnow() > deadline:
        return False
    return can("sales", "manage") if p.direction == "in" else can("supplier_pay", "yes")


def _target(p):
    """What a payment settles, and in which currency it is counted."""
    if p.sale_id:
        return p.sale, p.sale.currency
    if p.purchase_id:
        return p.purchase, p.purchase.currency
    return p.purchase_cost, p.purchase_cost.currency


def _tab_back(p):
    tab = "receipts" if p.sale_id else "payments"
    return _back_for(p) + "?tab=" + tab


_FIELDS = ("paid_on", "amount", "currency", "rate", "method", "reference", "note")


def _field_text(name, value):
    if value is None or value == "":
        return "—"
    if name == "amount":
        return format(Decimal(value).normalize(), "f")
    if name == "rate":
        return rate_field_value(value)
    if name == "paid_on":
        return value.isoformat()
    if name == "method":
        return fr("method." + value)
    return str(value)


@payments_bp.route("/<int:pay_id>/modifier", methods=["GET", "POST"])
@login_required
def edit(pay_id):
    """Correct a payment entered with a mistake. It keeps its receipt
    number, so a reprint reads the corrected figures under the same number."""
    payment = db.session.get(Payment, pay_id) or abort(404)
    t = get_t()
    back = _tab_back(payment)
    if not can_correct(payment):
        flash("error|" + t["payment.error.window_closed"])
        return modal_ok() if is_modal_request() else redirect(back)
    target, target_currency = _target(payment)
    mine = payment.amount_in(target_currency) or Decimal("0")
    balance = target.balance + mine           # what was due before this payment
    error = None
    if request.method == "POST":
        values, error = _parse_payment(target_currency)
        upload = None
        if error is None:
            upload, error = read_proof(required=payment.direction == "out" and not payment.proof_key)
        if error is None and upload and not attach_proof(payment, upload):
            error = t["doc.error.storage"]
        if error is None:
            changes = []
            for name in _FIELDS:
                before, after = getattr(payment, name), values[name]
                if _field_text(name, before) != _field_text(name, after):
                    changes.append("%s: %s → %s" % (name, _field_text(name, before), _field_text(name, after)))
                setattr(payment, name, after)
            if upload:
                changes.append("proof: %s" % payment.proof_filename)
            if changes:
                log_action("UPDATE", "receipt" if payment.sale_id else "payment", payment.id,
                           "%s · %s" % (_payment_label(payment), " · ".join(changes)))
            db.session.commit()
            flash("success|" + t["payment.updated"])
            in_target = convert(values["amount"], values["currency"], target_currency, values["rate"])
            if in_target is not None and in_target > balance + Decimal("0.01"):
                flash("warning|" + t["payment.warn.over_balance"])
            return modal_ok() if is_modal_request() else redirect(back)
    return form_response("payment_form.html", "_payment_form.html", error=error,
                         target_currency=target_currency, balance=balance,
                         default_rate=rate_field_value(payment.rate), payment=payment,
                         methods=PAYMENT_METHODS, form_action=url_for("payments.edit", pay_id=pay_id),
                         back_url=back, title=t["payment.edit"], default_method=payment.method,
                         proof_required=payment.direction == "out" and not payment.proof_key, **proof_form_context())


@payments_bp.route("/<int:pay_id>/supprimer", methods=["POST"])
@login_required
def delete(pay_id):
    """Delete a payment entered by mistake, while the window is open. The
    activity log keeps what it was."""
    payment = db.session.get(Payment, pay_id) or abort(404)
    t = get_t()
    back = _tab_back(payment)
    if not can_correct(payment):
        flash("error|" + t["payment.error.window_closed"])
        return redirect(back)
    log_action("DELETE", "receipt" if payment.sale_id else "payment", payment.id, "%s · %s · %s · %s%s" % (
        _payment_label(payment), payment.paid_on.isoformat(), money_text(payment.amount, payment.currency),
        fr("method." + payment.method), (" · " + payment.reference) if payment.reference else ""))
    key = payment.proof_key
    db.session.delete(payment)
    db.session.commit()
    if key:
        documents_store.remove(key)
    flash("success|" + t["payment.deleted"])
    return redirect(back)


def _may_see(p):
    from access import can
    return can("payments") or (can("sales") if p.direction == "in" else can("purchases"))


def _may_enter(p):
    from access import can
    return can("sales", "manage") if p.direction == "in" else can("supplier_pay", "yes")


@payments_bp.route("/<int:pay_id>/justificatif")
@login_required
def proof_file(pay_id):
    """The proof itself: a PDF or a photo opens in the browser."""
    payment = db.session.get(Payment, pay_id) or abort(404)
    if not _may_see(payment):
        abort(403)
    data = documents_store.load(payment.proof_key) if payment.proof_key else None
    if data is None:
        flash("error|" + get_t()["doc.error.missing"])
        return redirect(_tab_back(payment))
    inline = payment.proof_type in documents_store.SHOWN_INLINE and not request.args.get("download")
    resp = send_file(io.BytesIO(data), mimetype=payment.proof_type, as_attachment=not inline,
                     download_name=payment.proof_filename, max_age=0)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


@payments_bp.route("/<int:pay_id>/justificatif/joindre", methods=["GET", "POST"])
@login_required
def proof_attach(pay_id):
    """Attach the proof to a payment entered without one -- one entered
    before proofs were asked for, or a receipt whose paper comes later.
    Not bound to the correction window: the proof changes no figure."""
    payment = db.session.get(Payment, pay_id) or abort(404)
    if not _may_enter(payment):
        abort(403)
    t = get_t()
    back = _tab_back(payment)
    error = None
    if request.method == "POST":
        upload, error = read_proof(required=True)
        if error is None and not attach_proof(payment, upload):
            error = t["doc.error.storage"]
        if error is None:
            log_action("UPDATE", "receipt" if payment.sale_id else "payment", payment.id,
                       "%s · proof: %s" % (_payment_label(payment), payment.proof_filename))
            db.session.commit()
            flash("success|" + t["payment.proof_saved"])
            return modal_ok() if is_modal_request() else redirect(back)
    return form_response("proof_form.html", "_proof_form.html", payment=payment, error=error,
                         label=_payment_label(payment), back_url=back, **proof_form_context())


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
