"""Statistiques -- the dashboard's second tab: what sold, at what margin,
who buys, which suppliers deliver fast, where the road is slow, and what has
waited too long in the yard. Over a period chosen from a dropdown; worked
out from the rows each time, like everything else."""
from datetime import date

from flask import Blueprint, render_template, request
from flask_login import login_required

from analytics import (PERIODS, period_bounds, sales_figures, stage_delays,
                       stock_age, supplier_figures)
from app import current_rate, get_setting, get_t

stats_bp = Blueprint("stats", __name__, url_prefix="/statistiques")


@stats_bp.route("/")
@login_required
def index():
    t = get_t()
    today = date.today()
    period = request.args.get("period") or "12m"
    if period not in PERIODS:
        period = "12m"
    start, end = period_bounds(period, today)
    display = get_setting("display_currency", "USD") or "USD"
    rate = current_rate()
    sales = sales_figures(start, end, display, rate)
    return render_template(
        "stats.html", period=period, start=start, end=end, display=display, rate=rate,
        period_opts=[(p, t["stats.period." + p]) for p in PERIODS],
        sales=sales, suppliers=supplier_figures(start, end, display, rate),
        delays=stage_delays(start, end), stock=stock_age(today, display, rate),
        top_sold=max((r["sold"] for r in sales["models"]), default=0),
        top_revenue=max((r["revenue"] for _, r in sales["categories"]), default=0))
