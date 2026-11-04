"""The dashboard's figures, worked out from the rows each time.

Totals are shown in the display currency (Paramètres). Money already paid is
converted at the rate stored on each payment (the rate of its day); what is
still owed is valued at today's rate. Without a rate the two currencies are
never added: each is shown on its own line instead.
"""
from collections import OrderedDict
from datetime import date
from decimal import Decimal

from app import current_rate, get_setting
from models import (CURRENCIES, PURCHASE_STAGES, Payment, Purchase, Sale,
                    Unit, convert)

ZERO = Decimal("0")
STUCK_DAYS = 30
MONTHS = 12


def _add(bucket, currency, amount):
    bucket[currency] = bucket.get(currency, ZERO) + Decimal(amount or 0)


def in_display(per_currency, display, rate):
    """{currency: amount} as one figure in `display`, or None when a part in
    the other currency cannot be converted (no rate)."""
    total = ZERO
    for cur, amount in per_currency.items():
        v = convert(amount, cur, display, rate)
        if v is None:
            return None
        total += v
    return total


def figure(per_currency, display, rate):
    """What a stat tile shows: the converted total, and the parts by currency."""
    parts = [(c, per_currency[c]) for c in CURRENCIES if per_currency.get(c)]
    return {"total": in_display(per_currency, display, rate) if parts else ZERO,
            "parts": parts, "display": display}


def _months_back(today, n):
    out, y, m = [], today.year, today.month
    for _ in range(n):
        out.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(out))


def compute(today=None):
    from blueprints.sales import line_margin

    today = today or date.today()
    rate = current_rate()
    display = get_setting("display_currency", "USD") or "USD"

    # What customers owe, what we owe suppliers and agents.
    receivable, payable_goods, payable_costs = {}, {}, {}
    sales = Sale.query.filter(Sale.status != "cancelled").all()
    for s in sales:
        if s.balance > 0:
            _add(receivable, s.currency, s.balance)
    purchases = Purchase.query.all()
    for p in purchases:
        if p.balance > 0:
            _add(payable_goods, p.currency, p.balance)
        for c in p.costs:
            if c.balance > 0:
                _add(payable_costs, c.currency, c.balance)
    payable = dict(payable_goods)
    for cur, v in payable_costs.items():
        _add(payable, cur, v)

    # Money in and out this month, and over the last twelve months.
    months = _months_back(today, MONTHS)
    flow = OrderedDict(((y, m), {"in": ZERO, "out": ZERO, "unconverted": False}) for y, m in months)
    month_in, month_out = {}, {}
    first = date(months[0][0], months[0][1], 1)
    for pay in Payment.query.filter(Payment.voided_at.is_(None), Payment.paid_on >= first).all():
        key = (pay.paid_on.year, pay.paid_on.month)
        if key not in flow:
            continue
        v = convert(pay.amount, pay.currency, display, pay.rate or rate)
        if v is None:
            flow[key]["unconverted"] = True
        else:
            flow[key][pay.direction] += v
        if key == (today.year, today.month):
            _add(month_in if pay.direction == "in" else month_out, pay.currency, pay.amount)

    # Margin on what was delivered this year, at today's rate when currencies differ.
    margin, margin_missing = ZERO, 0
    delivered = [s for s in sales if s.status == "delivered" and s.delivered_on and s.delivered_on.year == today.year]
    for s in delivered:
        for line in s.lines:
            m = line_margin(line, s.currency)
            v = convert(m[0], s.currency, display, rate) if m else None
            if v is None:
                margin_missing += 1
            else:
                margin += v

    # The road and the yard.
    stage_counts = OrderedDict((st, 0) for st in PURCHASE_STAGES)
    for p in purchases:
        stage_counts[p.stage] += 1
    units = Unit.query.all()
    stock = {"in_stock_free": 0, "incoming_free": 0, "reserved": 0, "sold": 0, "delivered": 0}
    for u in units:
        st = u.status
        if st == "available":
            stock["in_stock_free" if u.purchase_line.purchase.stage == "in_stock" else "incoming_free"] += 1
        else:
            stock[st] += 1

    # What needs attention.
    stuck = []
    for p in purchases:
        if p.stage == "in_stock":
            continue
        if p.stage == "eta":
            # On the sea, waiting is normal until the estimated arrival.
            late = -p.eta_days(today) if p.eta_date else None
            if late and late > 0:
                stuck.append((p, late))
            continue
        d = p.stage_date(p.stage)
        if d and (today - d).days > STUCK_DAYS:
            stuck.append((p, (today - d).days))
    stuck.sort(key=lambda x: -x[1])
    no_vehicle = [s for s in sales if s.status in ("preorder", "confirmed") and any(l.unit_id is None for l in s.lines)]
    delivered_unpaid = [s for s in sales if s.status == "delivered" and s.balance > 0]

    chart = {
        "labels": ["%02d/%d" % (m, y % 100) for y, m in flow],
        "in": [float(v["in"]) for v in flow.values()],
        "out": [float(v["out"]) for v in flow.values()],
        "currency": display,
    }
    # This month's money in and out: each payment at its own day's rate, as
    # in the chart -- money already paid is not revalued at today's rate.
    this_month = flow.get((today.year, today.month))
    month_in_fig = figure(month_in, display, rate)
    month_out_fig = figure(month_out, display, rate)
    if this_month is not None:
        month_in_fig["total"] = None if this_month["unconverted"] else this_month["in"]
        month_out_fig["total"] = None if this_month["unconverted"] else this_month["out"]

    return {
        "rate": rate, "display": display,
        "receivable": figure(receivable, display, rate),
        "payable": figure(payable, display, rate),
        "payable_costs": figure(payable_costs, display, rate),
        "month_in": month_in_fig,
        "month_out": month_out_fig,
        "margin": margin, "margin_missing": margin_missing, "delivered_count": len(delivered),
        "stage_counts": stage_counts, "stock": stock,
        "stuck": stuck[:6], "no_vehicle": no_vehicle[:6], "no_vehicle_count": len(no_vehicle),
        "delivered_unpaid": delivered_unpaid[:6], "delivered_unpaid_count": len(delivered_unpaid),
        "chart": chart, "flow": flow,
        "unconverted_months": [k for k, v in flow.items() if v["unconverted"]],
        "has_data": bool(purchases or sales),
    }
