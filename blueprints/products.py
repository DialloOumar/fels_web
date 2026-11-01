"""Modèles — the second tab of the Vehicles page: the catalog of what is
bought and sold: "Shacman 430 HP",
"Fence trailer 16 m". Purchase and sale lines point here, so stock and
margins are counted per model. A model named on a purchase or a sale is
archived, never deleted."""
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import login_required

from analytics import model_stats
from app import form_response, get_t, is_modal_request, log_action, modal_ok, paginate
from blueprints.stock import tab_counts
from models import PRODUCT_CATEGORIES, Product, db, next_product_ref

products_bp = Blueprint("products", __name__, url_prefix="/modeles")


def _get_or_404(pid):
    p = db.session.get(Product, pid)
    if p is None:
        abort(404)
    return p


@products_bp.route("/")
@login_required
def index():
    show_archived = request.args.get("archived") == "1"
    search = (request.args.get("q") or "").strip()
    category = request.args.get("category") or ""
    q = Product.query.filter_by(is_active=not show_archived)
    if category in PRODUCT_CATEGORIES:
        q = q.filter_by(category=category)
    else:
        category = ""
    if search:
        like = "%" + search + "%"
        q = q.filter(db.or_(Product.name.ilike(like), Product.code.ilike(like), Product.ref.ilike(like),
                            Product.description.ilike(like)))
    pagination = paginate(q.order_by(Product.category, Product.ref, Product.name))
    archived_count = 0 if show_archived else Product.query.filter_by(is_active=False).count()
    t = get_t()
    return render_template("products.html", rows=pagination.items, pagination=pagination,
                           search=search, show_archived=show_archived, category=category,
                           category_opts=[(c, t["category." + c]) for c in PRODUCT_CATEGORIES],
                           archived_count=archived_count, tab_counts=tab_counts(),
                           stats=model_stats([p.id for p in pagination.items]))


def _save(row):
    t = get_t()
    name = (request.form.get("name") or "").strip()
    code = (request.form.get("code") or "").strip() or None
    category = request.form.get("category") or "other"
    description = (request.form.get("description") or "").strip() or None
    if not name:
        return t["product.error.name"]
    if category not in PRODUCT_CATEGORIES:
        return t["product.error.category"]
    clash = Product.query.filter(db.func.lower(Product.name) == name.lower())
    if row.id:
        clash = clash.filter(Product.id != row.id)
    if clash.first():
        return t["product.error.exists"]
    if code:
        clash = Product.query.filter(db.func.lower(Product.code) == code.lower())
        if row.id:
            clash = clash.filter(Product.id != row.id)
        if clash.first():
            return t["product.error.code_exists"]
    # The reference follows the category until the model is first bought or
    # sold; from then on it is written on its vehicles and stays.
    if row.ref is None or (category != row.category and not row.has_history):
        row.ref = next_product_ref(category)
    row.name, row.code, row.category, row.description = name, code, category, description
    is_new = row.id is None
    if is_new:
        db.session.add(row)
        db.session.flush()
    log_action("CREATE" if is_new else "UPDATE", "product", row.id, row.name)
    db.session.commit()
    return None


@products_bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    row, error = Product(category="other"), None
    if request.method == "POST":
        error = _save(row)
        if error is None:
            flash("success|" + get_t()["product.saved"])
            return modal_ok() if is_modal_request() else redirect(url_for("products.index"))
    return form_response("product_form.html", "_product_form.html", row=None, error=error,
                         categories=PRODUCT_CATEGORIES)


@products_bp.route("/<int:pid>/edit", methods=["GET", "POST"])
@login_required
def edit(pid):
    row, error = _get_or_404(pid), None
    if request.method == "POST":
        error = _save(row)
        if error is None:
            flash("success|" + get_t()["product.saved"])
            return modal_ok() if is_modal_request() else redirect(url_for("products.index"))
    return form_response("product_form.html", "_product_form.html", row=row, error=error,
                         categories=PRODUCT_CATEGORIES)


@products_bp.route("/<int:pid>/archive", methods=["POST"])
@login_required
def archive(pid):
    row = _get_or_404(pid)
    row.is_active = False
    log_action("ARCHIVE", "product", row.id, row.name)
    db.session.commit()
    flash("success|" + get_t()["archive.done"])
    return redirect(url_for("products.index"))


@products_bp.route("/<int:pid>/reactivate", methods=["POST"])
@login_required
def reactivate(pid):
    row = _get_or_404(pid)
    row.is_active = True
    log_action("RESTORE", "product", row.id, row.name)
    db.session.commit()
    flash("success|" + get_t()["archive.restored"])
    return redirect(url_for("products.index", archived=1))


@products_bp.route("/<int:pid>/delete", methods=["POST"])
@login_required
def delete(pid):
    row = _get_or_404(pid)
    t = get_t()
    if row.has_history:
        flash("error|" + t["delete.has_history"])
        return redirect(url_for("products.index", archived=1))
    log_action("DELETE", "product", row.id, row.name)
    db.session.delete(row)
    db.session.commit()
    flash("success|" + t["delete.done"])
    return redirect(url_for("products.index", archived=1))
