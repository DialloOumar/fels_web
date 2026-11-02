"""Figures worked out from the data each time, never stored: what each model
has been ordered and where its vehicles stand (the Models tab, and the
purchase form, so a model already on its way is not ordered twice
unnoticed), and what the Statistics page shows."""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import selectinload

from models import (PURCHASE_STAGES, STAGE_DATE_FIELD, Purchase, PurchaseLine,
                    Sale, SaleLine, Unit, convert, db)

ZERO = Decimal("0")


def _blank():
    return {"orders": 0, "ordered": 0, "last_order": None, "incoming": 0, "in_stock": 0,
            "reserved": 0, "delivered": 0, "waiting": 0, "free": 0}


def model_stats(product_ids=None, exclude_purchase=None):
    """{product_id: figures} for the given models, or all of them.

    orders      purchases naming the model;  ordered: vehicles ordered in all
    last_order  the latest of those invoice dates
    incoming    its vehicles still on the road, not delivered
    in_stock    its vehicles in the yard, not delivered
    reserved    of those two, how many a sale already holds
    delivered   handed over to a customer
    waiting     sale lines of the model still without a vehicle: sold ahead
    free        what is left to sell: incoming + in stock - reserved - waiting
                (below zero: sold more than ordered)

    `exclude_purchase` leaves one purchase out: the one being edited, so its
    own vehicles are not counted as already on their way.
    """
    out = defaultdict(_blank)
    orders = (db.session.query(PurchaseLine.product_id, db.func.count(db.distinct(PurchaseLine.purchase_id)),
                               db.func.sum(PurchaseLine.quantity), db.func.max(Purchase.invoice_date))
              .join(Purchase).group_by(PurchaseLine.product_id))
    units = (Unit.query.options(selectinload(Unit.sale_lines).selectinload(SaleLine.sale),
                                selectinload(Unit.purchase_line).selectinload(PurchaseLine.purchase)))
    waiting = (db.session.query(SaleLine.product_id, db.func.count(SaleLine.id)).join(Sale)
               .filter(SaleLine.unit_id.is_(None), Sale.status.in_(("preorder", "confirmed")))
               .group_by(SaleLine.product_id))
    if product_ids is not None:
        ids = list(product_ids) or [0]
        orders = orders.filter(PurchaseLine.product_id.in_(ids))
        units = units.filter(Unit.product_id.in_(ids))
        waiting = waiting.filter(SaleLine.product_id.in_(ids))
    if exclude_purchase:
        orders = orders.filter(PurchaseLine.purchase_id != exclude_purchase)
        units = units.join(PurchaseLine).filter(PurchaseLine.purchase_id != exclude_purchase)

    for pid, n, qty, last in orders:
        out[pid].update(orders=n, ordered=int(qty or 0), last_order=last)
    for u in units:
        s = out[u.product_id]
        status = u.status
        if status == "delivered":
            s["delivered"] += 1
            continue
        s["in_stock" if u.purchase_line.purchase.stage == "in_stock" else "incoming"] += 1
        if status == "reserved":
            s["reserved"] += 1
    for pid, n in waiting:
        out[pid]["waiting"] = n
    for s in out.values():
        s["free"] = s["incoming"] + s["in_stock"] - s["reserved"] - s["waiting"]
    return out


# ── The Statistics page ──────────────────────────────────────────────────────
#
# Money is shown in the display currency. A sale's price is converted at
# today's rate, like the dashboard's margin: an estimate for comparing, the
# books being the receipts and the costs. A figure that cannot be converted
# (no rate) is left out and counted, never guessed.

PERIODS = ("12m", "year", "last_year", "all")


def period_bounds(period, today):
    """(first day, last day) of a period; None for no bound."""
    if period == "year":
        return date(today.year, 1, 1), today
    if period == "last_year":
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    if period == "all":
        return None, None
    return today - timedelta(days=365), today


def _within(d, start, end):
    return d is not None and (start is None or d >= start) and (end is None or d <= end)


class _Landed:
    """Each vehicle's landed cost, its purchase's cost split worked out once."""

    def __init__(self):
        self.shares = {}

    def __call__(self, unit):
        line = unit.purchase_line
        p = line.purchase
        if p.id not in self.shares:
            self.shares[p.id] = p.extra_cost_per_unit()
        s = self.shares[p.id]
        if s is None:
            return None
        return Decimal(line.unit_price or 0) + s.get(line.id, ZERO), p.currency


def _row():
    return {"sold": 0, "revenue": ZERO, "margin": ZERO, "margin_revenue": ZERO, "margin_known": 0}


def _finish(r):
    r["avg_price"] = r["revenue"] / r["sold"] if r["sold"] else None
    r["avg_margin"] = r["margin"] / r["margin_known"] if r["margin_known"] else None
    r["margin_pct"] = (r["margin"] * 100 / r["margin_revenue"]) if r["margin_revenue"] else None
    return r


def sales_figures(start, end, display, rate):
    """What was sold in the period -- the lines of confirmed and delivered
    sales dated in it -- by model, by category and by customer, with the
    totals and how long vehicles waited in the yard before being sold."""
    landed = _Landed()
    total, by_model, by_cat, by_customer = _row(), defaultdict(_row), defaultdict(_row), {}
    unconverted, yard_days, sold_ahead = 0, [], 0
    sales = (Sale.query.filter(Sale.status.in_(("confirmed", "delivered")))
             .options(selectinload(Sale.lines).selectinload(SaleLine.unit)).all())
    for s in sales:
        if not _within(s.sale_date, start, end):
            continue
        c = by_customer.setdefault(s.customer_id, dict(_row(), customer=s.customer, sales=0, balance=ZERO))
        c["sales"] += 1
        balance = convert(s.balance, s.currency, display, rate)
        if balance is not None:
            c["balance"] += max(balance, ZERO)
        for line in s.lines:
            price = convert(line.price, s.currency, display, rate)
            if price is None:
                unconverted += 1
                continue
            margin = None
            if line.unit is not None:
                lc = landed(line.unit)
                cost = convert(lc[0], lc[1], display, rate) if lc else None
                if cost is not None:
                    margin = price - cost
                arrived = line.unit.purchase_line.purchase.in_stock_on
                if arrived and arrived <= s.sale_date:
                    yard_days.append((s.sale_date - arrived).days)
                else:
                    sold_ahead += 1
            for r in (total, by_model[line.product_id], by_cat[line.product.category], c):
                r["sold"] += 1
                r["revenue"] += price
                if margin is not None:
                    r["margin"] += margin
                    r["margin_revenue"] += price
                    r["margin_known"] += 1
            by_model[line.product_id]["product"] = line.product
    models = sorted((_finish(r) for r in by_model.values()), key=lambda r: (-r["sold"], -r["revenue"]))
    cats = sorted(((k, _finish(r)) for k, r in by_cat.items()), key=lambda kv: -kv[1]["revenue"])
    customers = sorted((_finish(r) for r in by_customer.values() if r["sold"]), key=lambda r: -r["revenue"])
    return {
        "total": _finish(total), "models": models, "categories": cats, "customers": customers[:10],
        "unconverted": unconverted,
        "yard_days": sum(yard_days) / len(yard_days) if yard_days else None, "sold_ahead": sold_ahead,
    }


def supplier_figures(start, end, display, rate):
    """Purchases invoiced in the period, by supplier: how many, how many
    vehicles, for how much, and how long the road took on average."""
    out = {}
    for p in Purchase.query.options(selectinload(Purchase.lines)).all():
        if not _within(p.invoice_date, start, end):
            continue
        r = out.setdefault(p.supplier_id, {"supplier": p.supplier, "purchases": 0, "vehicles": 0,
                                           "amount": ZERO, "unconverted": 0, "lead": []})
        r["purchases"] += 1
        r["vehicles"] += p.unit_count
        value = convert(p.goods_total, p.currency, display, rate)
        if value is None:
            r["unconverted"] += 1
        else:
            r["amount"] += value
        if p.ordered_on and p.in_stock_on:
            r["lead"].append((p.in_stock_on - p.ordered_on).days)
    for r in out.values():
        r["lead_days"] = sum(r["lead"]) / len(r["lead"]) if r["lead"] else None
    return sorted(out.values(), key=lambda r: -r["amount"])


def stage_delays(start, end):
    """The average days from each stage to the next, over the steps reached
    in the period; and from order to stock, door to door."""
    road = [s for s in PURCHASE_STAGES if s != "eta"]      # the estimate is not a step travelled
    steps = list(zip(road, road[1:]))
    gaps = {step: [] for step in steps}
    whole = []
    for p in Purchase.query.all():
        for a, b in steps:
            reached_a, reached_b = getattr(p, STAGE_DATE_FIELD[a]), getattr(p, STAGE_DATE_FIELD[b])
            if reached_a and reached_b and reached_b >= reached_a and _within(reached_b, start, end):
                gaps[(a, b)].append((reached_b - reached_a).days)
        if p.ordered_on and p.in_stock_on and _within(p.in_stock_on, start, end):
            whole.append((p.in_stock_on - p.ordered_on).days)
    rows = [{"from": a, "to": b, "days": sum(g) / len(g) if g else None, "n": len(g)} for (a, b), g in gaps.items()]
    return {"steps": rows, "door_to_door": sum(whole) / len(whole) if whole else None, "n": len(whole)}


AGE_BUCKETS = ((0, 30), (30, 60), (60, 90), (90, None))


def stock_age(today, display, rate):
    """The vehicles in the yard nobody has bought yet, by how long they have
    been there: money waiting. Not tied to the period -- it is today's yard."""
    landed = _Landed()
    buckets = [{"low": lo, "high": hi, "count": 0, "value": ZERO} for lo, hi in AGE_BUCKETS]
    waiting, unvalued = [], 0
    units = (Unit.query.options(selectinload(Unit.sale_lines).selectinload(SaleLine.sale),
                                selectinload(Unit.purchase_line).selectinload(PurchaseLine.purchase)).all())
    for u in units:
        p = u.purchase_line.purchase
        if p.stage != "in_stock" or u.status != "available":
            continue
        since = p.in_stock_on or p.invoice_date
        days = (today - since).days if since else 0
        lc = landed(u)
        value = convert(lc[0], lc[1], display, rate) if lc else None
        if value is None:
            unvalued += 1
        for b in buckets:
            if days >= b["low"] and (b["high"] is None or days < b["high"]):
                b["count"] += 1
                b["value"] += value or ZERO
        waiting.append((days, u, value))
    waiting.sort(key=lambda x: -x[0])
    return {"buckets": buckets, "oldest": waiting[:10], "unvalued": unvalued,
            "count": sum(b["count"] for b in buckets), "value": sum((b["value"] for b in buckets), ZERO)}
