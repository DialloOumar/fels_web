"""The bell: what needs a hand, read off the data itself.

Nothing here is stored. Each alert is worked out from purchases, sales and
stock every time the bell is drawn, so it disappears the moment the work is
done -- a vehicle assigned, a payment recorded, the rate re-entered -- and
can never go stale or need dismissing. (zone_web's bell keeps a table for
"things that happened to you"; that waits for the day there are several
users.)

The seven alerts, most useful first:

  fill        a vehicle of a model a customer is waiting for has reached the
              yard, while their sale still has no vehicle on that line
  stuck       a purchase at the same stage (port, customs...) for too long
  eta         a purchase still not at the port after its estimated arrival
  rate        the USD -> GNF rate is missing or has not been re-entered lately
  cost_rate   a cost in the other currency with no rate: no landed cost
  delivered   a delivered sale with money still to come in
  cleared     a purchase cleared or in stock with money still owed to the supplier

The thresholds (days) are settings: alert_stuck_days, alert_rate_days.
"""
from collections import OrderedDict
from datetime import date

from flask import Blueprint, jsonify, render_template, url_for
from flask_login import current_user, login_required

from app import app, current_rate, get_setting, get_t
from models import AppSetting, Purchase, PurchaseCost, Sale, Unit, db, utcnow

notifications_bp = Blueprint("notifications", __name__)

KINDS = OrderedDict([
    ("fill",      {"icon": "sell",              "level": "action"}),
    ("stuck",     {"icon": "schedule",          "level": "warn"}),
    ("eta",       {"icon": "directions_boat",   "level": "warn"}),
    ("rate",      {"icon": "currency_exchange", "level": "warn"}),
    ("cost_rate", {"icon": "assured_workload",  "level": "warn"}),
    ("delivered", {"icon": "payments",          "level": "info"}),
    ("cleared",   {"icon": "account_balance",   "level": "info"}),
])
PANEL_MAX = 10


def _int_setting(key, default):
    try:
        v = int(get_setting(key, default))
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def alerts(today=None):
    """Every alert, in the order of KINDS, then oldest first within a kind.
    Each: {kind, icon, level, url, params, key}."""
    today = today or date.today()
    out = []
    stuck_days = _int_setting("alert_stuck_days", 30)
    rate_days = _int_setting("alert_rate_days", 7)

    def add(kind, url, key, **params):
        out.append({"kind": kind, "icon": KINDS[kind]["icon"], "level": KINDS[kind]["level"],
                    "url": url, "params": params, "key": key})

    live_sales = Sale.query.filter(Sale.status.in_(("preorder", "confirmed", "delivered"))).order_by(Sale.sale_date, Sale.id).all()
    purchases = Purchase.query.order_by(Purchase.invoice_date, Purchase.id).all()

    # fill: free vehicles in the yard, by model, against lines waiting for one.
    free_in_yard = {}
    for u in Unit.query.all():
        if u.active_sale_line is None and u.purchase_line.purchase.stage == "in_stock":
            free_in_yard[u.product_id] = free_in_yard.get(u.product_id, 0) + 1
    for s in live_sales:
        if s.status == "delivered":
            continue
        for line in s.lines:
            if line.unit_id is None and free_in_yard.get(line.product_id, 0) > 0:
                free_in_yard[line.product_id] -= 1           # one vehicle answers one line
                add("fill", url_for("sales.edit", sid=s.id), "fill-%d" % line.id,
                    sale=s.number, customer=s.customer.name, model=line.product.code or line.product.name)

    # stuck: too long at one stage on the road.
    for p in purchases:
        if p.stage == "in_stock":
            continue
        if p.stage == "eta":
            # On the sea the estimate is the measure, not the days in stage.
            if p.eta_date and p.eta_date < today:
                add("eta", url_for("purchases.detail", pid=p.id), "eta-%d" % p.id,
                    number=p.invoice_number, date=p.eta_date.strftime("%d/%m/%Y"), days=(today - p.eta_date).days)
            continue
        d = p.stage_date(p.stage)
        if d and (today - d).days > stuck_days:
            add("stuck", url_for("purchases.detail", pid=p.id), "stuck-%d" % p.id,
                number=p.invoice_number, stage=p.stage, days=(today - d).days)

    # rate: missing, or not re-entered for a while.
    row = db.session.get(AppSetting, "usd_gnf_rate")
    if current_rate() is None:
        add("rate", url_for("settings.index"), "rate-missing", days=None)
    elif row is not None and row.updated_at and (utcnow() - row.updated_at).days >= rate_days:
        add("rate", url_for("settings.index"), "rate-old", days=(utcnow() - row.updated_at).days)

    # cost_rate: a cost that cannot be turned into the purchase's currency.
    for c in PurchaseCost.query.filter(PurchaseCost.rate.is_(None)).all():
        if c.currency != c.purchase.currency:
            add("cost_rate", url_for("purchases.detail", pid=c.purchase_id), "cost-%d" % c.id,
                number=c.purchase.invoice_number)

    # delivered: handed over, not fully paid.
    for s in live_sales:
        if s.status == "delivered" and s.balance > 0:
            add("delivered", url_for("sales.detail", sid=s.id), "delivered-%d" % s.id,
                sale=s.number, customer=s.customer.name, amount=(s.balance, s.currency))

    # cleared: released at the port, still owed to the supplier.
    for p in purchases:
        if p.stage in ("cleared", "in_stock") and p.balance > 0:
            add("cleared", url_for("purchases.detail", pid=p.id), "cleared-%d" % p.id,
                number=p.invoice_number, supplier=p.supplier.name, amount=(p.balance, p.currency))
    # Each alert to whoever may act on it.
    from access import can
    allowed = {"fill": can("sales", "manage"), "stuck": can("purchases"), "eta": can("purchases"),
               "rate": can("settings", "yes"),
               "cost_rate": can("purchases"), "delivered": can("sales"), "cleared": can("purchases")}
    return [a for a in out if allowed.get(a["kind"])]


def alert_text(a):
    """One alert as a sentence, in the reader's language."""
    t = get_t()
    p = dict(a["params"])
    if "amount" in p:
        amount, cur = p["amount"]
        p["amount"] = app.jinja_env.filters["money"](amount, cur)
    if "stage" in p:
        p["stage"] = t["stage." + p["stage"]]
    key = "alert.%s" % a["kind"]
    if a["kind"] == "rate":
        key += ".missing" if p.get("days") is None else ".old"
    return t[key] % p


_REQUEST_CACHE = "_fels_alerts"


def bell():
    """What the bell shows, worked out once per request."""
    from flask import g
    if not current_user.is_authenticated:
        return {"count": 0, "items": [], "more": 0}
    if not hasattr(g, _REQUEST_CACHE):
        items = alerts()
        setattr(g, _REQUEST_CACHE, {"count": len(items), "items": items[:PANEL_MAX],
                                    "more": max(0, len(items) - PANEL_MAX)})
    return getattr(g, _REQUEST_CACHE)


@app.context_processor
def _inject_bell():
    return {"notif_bell": bell, "alert_text": alert_text}


@notifications_bp.route("/notifications/count")
@login_required
def count():
    """The number on the bell, asked for in the background every 30 seconds."""
    return jsonify(count=bell()["count"])


@notifications_bp.route("/notifications/panel")
@login_required
def panel():
    """The bell's list, drawn again each time it is opened."""
    return render_template("_bell_panel.html", b=bell())


@notifications_bp.route("/alertes")
@login_required
def index():
    """Every alert, grouped by kind, with what each kind means."""
    groups = OrderedDict((k, []) for k in KINDS)
    for a in alerts():
        groups[a["kind"]].append(a)
    return render_template("alerts.html", groups=groups, kinds=KINDS,
                           stuck_days=_int_setting("alert_stuck_days", 30),
                           rate_days=_int_setting("alert_rate_days", 7))
