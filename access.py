"""Who may do what: roles, their permissions, and the one place they are
enforced.

A role grants each AREA a level. Most areas have three: none, view (read
the pages) and manage (create, edit, delete, act). A few are a plain yes or
no: running the import, recording payments to suppliers, voiding a payment.
A user holds one role; the super admin holds them all, and alone manages
users and roles.

Enforcement is central rather than scattered over the views: every endpoint
of the app is listed in RULES with the permission it needs, or in OPEN when
any signed-in person may use it. An endpoint in neither is refused -- a page
added later and forgotten here fails closed, and the test suite checks that
nothing is left unlisted. Exporting a list to Excel (?format=xlsx) needs the
export permission on top of the list's own.
"""
from collections import OrderedDict

from flask import abort, request
from flask_login import current_user

# area -> its levels, lowest first ("none" is implied below them all)
AREAS = OrderedDict([
    ("dashboard",    ("view",)),
    ("purchases",    ("view", "manage")),
    ("supplier_pay", ("yes",)),
    ("sales",        ("view", "manage")),
    ("stock",        ("view", "manage")),
    ("suppliers",    ("view", "manage")),
    ("customers",    ("view", "manage")),
    ("products",     ("view", "manage")),
    ("payments",     ("view",)),
    ("void",         ("yes",)),
    ("export",       ("yes",)),
    ("import",       ("yes",)),
    ("settings",     ("yes",)),
])

# Pages any signed-in person may open: their own account, the shell.
OPEN = {
    "static", "login", "logout", "healthz", "set_language", "set_theme",
    "change_password", "dashboard",          # the dashboard redirects by itself
    "signature.mine", "signature.stamp_svg",
    "notifications.count", "notifications.panel", "notifications.index",
}

# Pages only the super admin opens: people and roles.
SUPER_ONLY_BLUEPRINTS = {"users"}

V, M, Y = "view", "manage", "yes"
RULES = {
    # settings, ports, error journal
    "settings.index": ("settings", Y), "settings.port_new": ("settings", Y),
    "settings.port_edit": ("settings", Y), "settings.port_archive": ("settings", Y),
    "settings.port_delete": ("settings", Y), "settings.errors": ("settings", Y),
    # suppliers
    "suppliers.index": ("suppliers", V), "suppliers.detail": ("suppliers", V),
    "suppliers.new": ("suppliers", M), "suppliers.edit": ("suppliers", M),
    "suppliers.archive": ("suppliers", M), "suppliers.reactivate": ("suppliers", M),
    "suppliers.delete": ("suppliers", M),
    # customers
    "customers.index": ("customers", V), "customers.detail": ("customers", V),
    "customers.statement": ("customers", V),
    "customers.new": ("customers", M), "customers.edit": ("customers", M),
    "customers.archive": ("customers", M), "customers.reactivate": ("customers", M),
    "customers.delete": ("customers", M),
    # products (the Models tab)
    "products.index": ("products", V),
    "products.new": ("products", M), "products.edit": ("products", M),
    "products.archive": ("products", M), "products.reactivate": ("products", M),
    "products.delete": ("products", M),
    # purchases, the pipeline, costs
    "purchases.index": ("purchases", V), "purchases.pipeline": ("purchases", V),
    "purchases.detail": ("purchases", V),
    "purchases.new": ("purchases", M), "purchases.edit": ("purchases", M),
    "purchases.delete": ("purchases", M), "purchases.stage": ("purchases", M),
    "purchases.cost_new": ("purchases", M), "purchases.cost_edit": ("purchases", M),
    "purchases.cost_delete": ("purchases", M),
    # money
    "payments.purchase_payment": ("supplier_pay", Y), "payments.cost_payment": ("supplier_pay", Y),
    "payments.void": ("void", Y),
    "payments.index": ("payments", V),
    # stock
    "stock.index": ("stock", V), "stock.edit": ("stock", M),
    # sales and receipts
    "sales.index": ("sales", V), "sales.detail": ("sales", V), "sales.receipt_print": ("sales", V),
    "sales.new": ("sales", M), "sales.edit": ("sales", M), "sales.confirm": ("sales", M),
    "sales.deliver": ("sales", M), "sales.undeliver": ("sales", M), "sales.cancel": ("sales", M),
    "sales.receipt_new": ("sales", M),
    # the Excel import
    "importer.upload": ("import", Y), "importer.preview": ("import", Y),
    "importer.confirm": ("import", Y), "importer.discard": ("import", Y),
}


def _rank(area, level):
    levels = AREAS.get(area, ())
    return levels.index(level) + 1 if level in levels else 0


def role_grants(permissions, area, level="view"):
    """Whether a role's {area: level} reaches `level` in `area`. Manage
    includes view."""
    held = (permissions or {}).get(area)
    if not held:
        return False
    need = level if level in AREAS.get(area, ()) else AREAS.get(area, ("",))[0]
    return _rank(area, held) >= _rank(area, need)


def can(area, level="view"):
    """Whether the current user may do `level` in `area`."""
    if not current_user.is_authenticated:
        return False
    if current_user.is_super_admin:
        return True
    role = current_user.role
    return bool(role) and role_grants(role.permissions, area, level)


def home_url():
    """Where a person lands after signing in: the first page they may open."""
    from flask import url_for
    for area, endpoint in (("dashboard", "dashboard"), ("sales", "sales.index"),
                           ("purchases", "purchases.index"), ("stock", "stock.index"),
                           ("customers", "customers.index"), ("suppliers", "suppliers.index")):
        if can(area):
            return url_for(endpoint)
    return url_for("signature.mine")


def enforce():
    """before_request: refuse what the current user's role does not allow.
    Anonymous requests are left to login_required, which redirects."""
    endpoint = request.endpoint
    if endpoint is None or not current_user.is_authenticated or current_user.is_super_admin:
        return
    blueprint = endpoint.split(".", 1)[0] if "." in endpoint else None
    if blueprint in SUPER_ONLY_BLUEPRINTS:
        abort(403)
    if endpoint in OPEN:
        return
    rule = RULES.get(endpoint)
    if rule is None:
        abort(403)                           # unlisted: fail closed
    if not can(*rule):
        abort(403)
    if request.args.get("format") == "xlsx" and not can("export", "yes"):
        abort(403)


# The one role created on a fresh install: everything a role can grant, at
# its highest level. What stays outside any role -- users and roles -- is the
# super admin's alone (SUPER_ONLY_BLUEPRINTS). Other roles are made by hand.
SEED_ROLES = [
    ("Manager", "Accès complet, sauf les utilisateurs et les rôles (réservés à l'administrateur).",
     {area: levels[-1] for area, levels in AREAS.items()}),
]
