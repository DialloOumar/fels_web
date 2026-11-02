"""SQLAlchemy models for fels_web.

The business: heavy vehicles (tractors, trailers, pickups) bought from one or
more suppliers abroad, shipped to a Guinean port, cleared through customs,
then sold to local customers -- often before they arrive, against a deposit.

Schema overview:

  Access        User, Role
  Config        AppSetting, Port, ErrorEvent
  Catalog       Product
  Buying        Supplier, Purchase, PurchaseLine, PurchaseCost
  Stock         Unit                       (one row per physical vehicle)
  Selling       Customer, Sale, SaleLine
  Money         Payment                    (in from customers, out to suppliers / costs)

Money rules, everywhere:
  * Every amount carries its own currency (USD or GNF). Nothing is converted
    when it is stored.
  * Anything that converts stores the rate it used (GNF per 1 USD), copied
    from the settings on the day and editable. Changing the rate later never
    rewrites an old figure.
  * Totals, amounts paid, balances and statuses are computed from the rows
    beneath them, never typed and never stored.
"""
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()

CURRENCIES = ("USD", "GNF")
MONEY = db.Numeric(18, 2)
RATE = db.Numeric(18, 4)          # GNF per 1 USD
CENT = Decimal("0.01")
ZERO = Decimal("0")


def utcnow():
    """Naive UTC, as the DateTime columns store it."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def convert(amount, from_cur, to_cur, rate):
    """`amount` in `from_cur`, expressed in `to_cur`, at `rate` GNF per USD.

    Same currency: returned as is. Without a usable rate a conversion is
    impossible, and None says so rather than a silently wrong zero.
    """
    amount = Decimal(amount or 0)
    if from_cur == to_cur:
        return amount
    rate = Decimal(rate or 0)
    if rate <= 0:
        return None
    if from_cur == "USD" and to_cur == "GNF":
        return (amount * rate).quantize(CENT, rounding=ROUND_HALF_UP)
    if from_cur == "GNF" and to_cur == "USD":
        return (amount / rate).quantize(CENT, rounding=ROUND_HALF_UP)
    return None


# ── Access ───────────────────────────────────────────────────────────────────


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id             = db.Column(db.Integer, primary_key=True)
    username       = db.Column(db.String(80),  nullable=False, unique=True)
    full_name      = db.Column(db.String(120), nullable=False)
    email          = db.Column(db.String(120))
    password_hash  = db.Column(db.String(256), nullable=False)
    # The one super admin, bootstrapped from the environment by `flask seed`.
    # Holds every permission and alone manages users and roles.
    is_super_admin = db.Column(db.Boolean, nullable=False, default=False)
    # Everyone else: what their role allows (see access.py). No role, no access
    # beyond their own account.
    role_id        = db.Column(db.Integer, db.ForeignKey("roles.id"), index=True)
    lang           = db.Column(db.String(5), nullable=False, default="fr")
    is_active      = db.Column(db.Boolean, nullable=False, default=True)
    last_login_at  = db.Column(db.DateTime)
    # Their stamp, printed on the receipts they issue: the word in the middle
    # ("Direction"), the phone on the lower arc, and the strokes of their
    # signature as a transparent PNG laid over it (see signatures.py).
    stamp_label    = db.Column(db.String(40))
    stamp_phone    = db.Column(db.String(30))
    signature_png  = db.Column(db.LargeBinary)
    signature_at   = db.Column(db.DateTime)
    # Where the strokes sit on the stamp: a shift in stamp units (the stamp is
    # 300 wide) and a size in percent, as the person set them.
    sig_dx         = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    sig_dy         = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    sig_scale      = db.Column(db.Integer, nullable=False, default=100, server_default="100")
    stamp_color    = db.Column(db.String(7))     # #rrggbb; empty = the stamp's blue
    sig_color      = db.Column(db.String(7))     # #rrggbb; empty = same as the stamp
    created_at     = db.Column(db.DateTime, nullable=False, default=utcnow)

    role = db.relationship("Role", back_populates="users")

    def set_password(self, raw):
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw):
        return check_password_hash(self.password_hash, raw)


class Role(db.Model):
    """A set of permissions given to users: {area: level}, the areas and
    their levels being those of access.AREAS ("purchases": "manage"...)."""
    __tablename__ = "roles"

    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(60), nullable=False, unique=True)
    description = db.Column(db.String(255))
    permissions = db.Column(db.JSON, nullable=False, default=dict)
    created_at  = db.Column(db.DateTime, nullable=False, default=utcnow)

    users = db.relationship("User", back_populates="role", order_by="User.full_name")


# ── Config ───────────────────────────────────────────────────────────────────


class AppSetting(db.Model):
    """Key/value settings edited on the Paramètres page: the USD rate, the
    display currency, the company details printed on receipts."""
    __tablename__ = "app_settings"

    key        = db.Column(db.String(50), primary_key=True)
    value      = db.Column(db.Text, nullable=False, default="")
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class Port(db.Model):
    """Where a purchase lands: Boffa, Boké. A short list, so "everything at
    Boffa" is a filter and not a search through spellings."""
    __tablename__ = "ports"

    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(60), nullable=False, unique=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    is_active  = db.Column(db.Boolean, nullable=False, default=True)

    @property
    def has_history(self):
        return db.session.query(Purchase.id).filter_by(port_id=self.id).first() is not None


class ErrorEvent(db.Model):
    """One unexpected error, under the reference the user was shown
    (ERR-7F3A2C), with its traceback. Kept 90 days."""
    __tablename__ = "error_events"

    id         = db.Column(db.Integer, primary_key=True)
    ref        = db.Column(db.String(12), nullable=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    method     = db.Column(db.String(10))
    path       = db.Column(db.String(300))
    user_id    = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    username   = db.Column(db.String(80))
    message    = db.Column(db.String(300))
    traceback  = db.Column(db.Text)
    form_keys  = db.Column(db.String(300))


class AuditLog(db.Model):
    """Who did what, and when: one row per change, sign-in and failed
    sign-in. The name and role are copied at the time, so the trail reads the
    same after a user is renamed, changes role or is deactivated. Written by
    log_action() in the same transaction as the change it describes."""
    __tablename__ = "audit_logs"

    id            = db.Column(db.Integer, primary_key=True)
    timestamp     = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    user_id       = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), index=True)
    username      = db.Column(db.String(80), nullable=False)     # snapshot
    actor_role    = db.Column(db.String(80))                     # snapshot
    action        = db.Column(db.String(30), nullable=False, index=True)   # CREATE | UPDATE | DELETE | LOGIN | …
    resource_type = db.Column(db.String(40), nullable=False, index=True)
    resource_id   = db.Column(db.Integer)
    detail        = db.Column(db.String(255))
    ip_address    = db.Column(db.String(45))


# ── Catalog ──────────────────────────────────────────────────────────────────

PRODUCT_CATEGORIES = ("tractor", "trailer", "truck", "pickup", "other")
# Each category's prefix in the references the app gives: a model is TRC-001,
# its vehicles TRC-001-01, TRC-001-02… (TRacteur, REMorque, CAMion, PicKuP, DIVers).
CATEGORY_PREFIX = {"tractor": "TRC", "trailer": "REM", "truck": "CAM", "pickup": "PKP", "other": "DIV"}


class Product(db.Model):
    """A model the business buys and sells: "Shacman 430 HP", "Fence trailer
    16 m". Purchase and sale lines point here instead of carrying free text,
    so stock and margins can be counted per model."""
    __tablename__ = "products"

    id          = db.Column(db.Integer, primary_key=True)
    ref         = db.Column(db.String(12), unique=True)            # TRC-001, given by the app
    unit_seq    = db.Column(db.Integer, nullable=False, default=0, server_default="0")  # last vehicle number given
    name        = db.Column(db.String(120), nullable=False, unique=True)
    code        = db.Column(db.String(20), unique=True)            # short form: SHM430, FcT16
    category    = db.Column(db.String(12), nullable=False, default="other")
    description = db.Column(db.String(255))
    is_active   = db.Column(db.Boolean, nullable=False, default=True)
    created_at  = db.Column(db.DateTime, nullable=False, default=utcnow)

    @property
    def has_history(self):
        """Named on a purchase or a sale: from then on it is archived, never
        deleted, so the old lines keep their meaning."""
        return (db.session.query(PurchaseLine.id).filter_by(product_id=self.id).first() is not None
                or db.session.query(SaleLine.id).filter_by(product_id=self.id).first() is not None)


def next_product_ref(category):
    """The next free reference in a category: TRC-001, TRC-002…"""
    prefix = CATEGORY_PREFIX.get(category, CATEGORY_PREFIX["other"]) + "-"
    taken = db.session.query(Product.ref).filter(Product.ref.like(prefix + "%")).all()
    numbers = [int(r[len(prefix):]) for (r,) in taken if r[len(prefix):].isdigit()]
    return "%s%03d" % (prefix, max(numbers, default=0) + 1)


def assign_unit_refs():
    """Number every vehicle still without a reference, model by model, after
    the ones already numbered: TRC-001-01, TRC-001-02… A number is never
    reused, so a reference written on a paper stays that vehicle's. Called
    before the commit of whatever created or re-modelled vehicles."""
    db.session.flush()
    for u in Unit.query.filter(Unit.ref.is_(None)).order_by(Unit.id).all():
        p = u.product
        if not p.ref:
            p.ref = next_product_ref(p.category)
        p.unit_seq = (p.unit_seq or 0) + 1
        u.seq = p.unit_seq
        u.ref = "%s-%02d" % (p.ref, u.seq)


# ── Buying ───────────────────────────────────────────────────────────────────


class Supplier(db.Model):
    """Who the vehicles are bought from. The default currency pre-fills a
    new purchase: a supplier in China bills in USD."""
    __tablename__ = "suppliers"

    id               = db.Column(db.Integer, primary_key=True)
    name             = db.Column(db.String(120), nullable=False, unique=True)
    country          = db.Column(db.String(60))
    contact_name     = db.Column(db.String(120))
    phone            = db.Column(db.String(60))
    email            = db.Column(db.String(120))
    address          = db.Column(db.String(255))
    default_currency = db.Column(db.String(3), nullable=False, default="USD")
    note             = db.Column(db.Text)
    is_active        = db.Column(db.Boolean, nullable=False, default=True)
    created_at       = db.Column(db.DateTime, nullable=False, default=utcnow)

    purchases = db.relationship("Purchase", back_populates="supplier",
                                order_by="Purchase.invoice_date.desc()")

    @property
    def has_history(self):
        return bool(self.purchases)


# The road a purchase travels, in order. Each stage has the date it was
# reached, so the time spent at the port or in customs can be read off.
# "eta" is reached when the supplier gives the estimated arrival at the port,
# some time after shipping; the estimate itself is Purchase.eta_date.
PURCHASE_STAGES = ("ordered", "shipped", "eta", "at_port", "customs", "cleared", "in_stock")
STAGE_DATE_FIELD = {
    "ordered":  "ordered_on",
    "shipped":  "shipped_on",
    "eta":      "eta_on",
    "at_port":  "at_port_on",
    "customs":  "customs_on",
    "cleared":  "cleared_on",
    "in_stock": "in_stock_on",
}


class Purchase(db.Model):
    """One supplier invoice: what was ordered, from whom, landing where.

    Its total is the sum of its lines. What has been paid on it is the sum of
    its payments, each converted at its own rate; the balance follows.
    """
    __tablename__ = "purchases"
    __table_args__ = (
        db.UniqueConstraint("supplier_id", "invoice_number", name="uq_purchase_supplier_invoice"),
    )

    id             = db.Column(db.Integer, primary_key=True)
    supplier_id    = db.Column(db.Integer, db.ForeignKey("suppliers.id"), nullable=False, index=True)
    invoice_number = db.Column(db.String(60), nullable=False)     # the supplier's own number
    invoice_date   = db.Column(db.Date, nullable=False)
    port_id        = db.Column(db.Integer, db.ForeignKey("ports.id"), index=True)
    currency       = db.Column(db.String(3), nullable=False, default="USD")
    stage          = db.Column(db.String(12), nullable=False, default="ordered", index=True)
    ordered_on     = db.Column(db.Date)
    shipped_on     = db.Column(db.Date)
    eta_on         = db.Column(db.Date)                       # when the estimate was given
    eta_date       = db.Column(db.Date)                       # the estimated arrival at the port
    at_port_on     = db.Column(db.Date)
    customs_on     = db.Column(db.Date)
    cleared_on     = db.Column(db.Date)
    in_stock_on    = db.Column(db.Date)
    note           = db.Column(db.Text)
    created_by     = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    created_at     = db.Column(db.DateTime, nullable=False, default=utcnow)

    supplier = db.relationship("Supplier", back_populates="purchases")
    port     = db.relationship("Port")
    lines    = db.relationship("PurchaseLine", back_populates="purchase",
                               cascade="all, delete-orphan", order_by="PurchaseLine.id")
    costs    = db.relationship("PurchaseCost", back_populates="purchase",
                               cascade="all, delete-orphan", order_by="PurchaseCost.incurred_on")
    payments = db.relationship("Payment", back_populates="purchase",
                               order_by="Payment.paid_on")
    documents = db.relationship("PurchaseDocument", back_populates="purchase", cascade="all, delete-orphan",
                                order_by=lambda: (PurchaseDocument.doc_date.desc().nullslast(),
                                                  PurchaseDocument.id.desc()))

    @property
    def goods_total(self):
        return sum((l.total for l in self.lines), ZERO)

    @property
    def paid_amount(self):
        """Payments to the supplier, in the purchase's currency."""
        return sum((p.amount_in(self.currency) or ZERO
                    for p in self.payments if not p.is_voided), ZERO)

    @property
    def balance(self):
        return max(self.goods_total - self.paid_amount, ZERO)

    @property
    def pay_status(self):
        """paid | partial | unpaid -- read off the payments, never stored."""
        if self.goods_total > 0 and self.balance <= 0:
            return "paid"
        return "partial" if self.paid_amount > 0 else "unpaid"

    @property
    def unit_count(self):
        return sum(l.quantity or 0 for l in self.lines)

    def costs_total(self, currency=None):
        """The extra costs (customs, port, transit...) in one currency, each
        converted at its own rate. None when one cannot be converted."""
        currency = currency or self.currency
        total = ZERO
        for c in self.costs:
            v = c.amount_in(currency)
            if v is None:
                return None
            total += v
        return total

    def extra_cost_per_unit(self):
        """{line_id: share of the extra costs carried by ONE unit of that
        line}, in the purchase's currency.

        The costs are spread in proportion to price: a line worth twice as
        much carries twice the share, so a tractor bears more duty than a
        trailer. Lines with no price share nothing. Returns None when a cost
        cannot be converted (no rate)."""
        extra = self.costs_total(self.currency)
        if extra is None:
            return None
        goods = self.goods_total
        shares = {}
        for l in self.lines:
            if not l.quantity or goods <= 0:
                shares[l.id] = ZERO
                continue
            line_share = extra * (l.total / goods)
            shares[l.id] = (line_share / l.quantity).quantize(CENT, rounding=ROUND_HALF_UP)
        return shares

    def stage_date(self, stage):
        return getattr(self, STAGE_DATE_FIELD[stage])

    def eta_days(self, today):
        """Days until the estimated arrival (negative once it has passed);
        None without an estimate."""
        return (self.eta_date - today).days if self.eta_date else None

    @property
    def eta_gap(self):
        """Once at the port: how many days after the estimate it arrived
        (negative: early). What says whether a supplier's estimates hold."""
        if self.eta_date and self.at_port_on:
            return (self.at_port_on - self.eta_date).days
        return None


class PurchaseLine(db.Model):
    """So many of one model at one price. Each unit it brings in becomes a
    Unit row, the physical vehicle."""
    __tablename__ = "purchase_lines"

    id          = db.Column(db.Integer, primary_key=True)
    purchase_id = db.Column(db.Integer, db.ForeignKey("purchases.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id  = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)
    quantity    = db.Column(db.Integer, nullable=False, default=1)
    unit_price  = db.Column(MONEY, nullable=False, default=0)       # in the purchase's currency
    description = db.Column(db.String(255))

    purchase = db.relationship("Purchase", back_populates="lines")
    product  = db.relationship("Product")
    units    = db.relationship("Unit", back_populates="purchase_line",
                               cascade="all, delete-orphan", order_by="Unit.id")

    @property
    def total(self):
        return (Decimal(self.unit_price or 0) * (self.quantity or 0)).quantize(CENT)


COST_KINDS = ("customs_duty", "port_fees", "transit", "transport", "other")


# The papers a purchase gathers on its way, roughly in the order they come.
# The list is the code's; "other" takes a title for anything not in it.
DOCUMENT_KINDS = ("supplier_invoice", "packing_list", "bill_of_lading", "certificate_of_origin",
                  "customs_declaration", "customs_receipt", "delivery_note", "other")


class PurchaseDocument(db.Model):
    """A file attached to a purchase: the supplier's invoice, the bill of
    lading, the customs receipt… The bytes live in documents_store, under
    storage_key; this row says what the file is."""
    __tablename__ = "purchase_documents"

    id           = db.Column(db.Integer, primary_key=True)
    purchase_id  = db.Column(db.Integer, db.ForeignKey("purchases.id", ondelete="CASCADE"), nullable=False, index=True)
    kind         = db.Column(db.String(30), nullable=False)
    title        = db.Column(db.String(120))               # required for "other", a detail otherwise
    number       = db.Column(db.String(60))                # BL no., declaration no.…
    doc_date     = db.Column(db.Date)
    filename     = db.Column(db.String(200), nullable=False)   # as uploaded, for the download
    content_type = db.Column(db.String(100), nullable=False)
    size         = db.Column(db.Integer, nullable=False)
    storage_key  = db.Column(db.String(255), nullable=False, unique=True)
    uploaded_by  = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    created_at   = db.Column(db.DateTime, nullable=False, default=utcnow)

    purchase = db.relationship("Purchase", back_populates="documents")
    uploader = db.relationship("User")


class PurchaseCost(db.Model):
    """What a purchase costs on top of the supplier's price: customs duty,
    port fees, the transit agent, the truck to the yard. Spread over the
    purchase's units to give each its landed cost."""
    __tablename__ = "purchase_costs"

    id          = db.Column(db.Integer, primary_key=True)
    purchase_id = db.Column(db.Integer, db.ForeignKey("purchases.id", ondelete="CASCADE"), nullable=False, index=True)
    kind        = db.Column(db.String(20), nullable=False, default="other")
    description = db.Column(db.String(255))
    payee       = db.Column(db.String(120))                       # the agent or authority paid
    amount      = db.Column(MONEY, nullable=False)
    currency    = db.Column(db.String(3), nullable=False, default="GNF")
    rate        = db.Column(RATE)                                 # GNF per USD on that day
    incurred_on = db.Column(db.Date, nullable=False)
    reference   = db.Column(db.String(60))                        # declaration no., receipt no.
    created_at  = db.Column(db.DateTime, nullable=False, default=utcnow)

    purchase = db.relationship("Purchase", back_populates="costs")
    payments = db.relationship("Payment", back_populates="purchase_cost", order_by="Payment.paid_on")

    def amount_in(self, currency):
        return convert(self.amount, self.currency, currency, self.rate)

    @property
    def paid_amount(self):
        return sum((p.amount_in(self.currency) or ZERO
                    for p in self.payments if not p.is_voided), ZERO)

    @property
    def balance(self):
        return max(Decimal(self.amount or 0) - self.paid_amount, ZERO)


# ── Stock ────────────────────────────────────────────────────────────────────


class Unit(db.Model):
    """One physical vehicle, born from a purchase line. The chassis number is
    optional: it may only be known once the vehicle is at the port.

    Where it stands commercially is read off the sales naming it, never
    stored: available, reserved (on a pre-order or a confirmed sale) or
    delivered."""
    __tablename__ = "units"

    id               = db.Column(db.Integer, primary_key=True)
    purchase_line_id = db.Column(db.Integer, db.ForeignKey("purchase_lines.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id       = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)
    seq              = db.Column(db.Integer)                         # its number within the model
    ref              = db.Column(db.String(20), unique=True)         # TRC-001-07, given by the app
    chassis_number   = db.Column(db.String(60), unique=True)
    note             = db.Column(db.String(255))
    created_at       = db.Column(db.DateTime, nullable=False, default=utcnow)

    purchase_line = db.relationship("PurchaseLine", back_populates="units")
    product       = db.relationship("Product")
    sale_lines    = db.relationship("SaleLine", back_populates="unit")

    @property
    def active_sale_line(self):
        for sl in self.sale_lines:
            if sl.sale and sl.sale.status != "cancelled":
                return sl
        return None

    @property
    def status(self):
        sl = self.active_sale_line
        if sl is None:
            return "available"
        return "delivered" if sl.sale.status == "delivered" else "reserved"

    @property
    def landed_cost(self):
        """(amount, currency): the line's unit price plus this vehicle's
        share of its purchase's extra costs, in the purchase's currency.
        None while a cost has no rate to convert it with."""
        line = self.purchase_line
        shares = line.purchase.extra_cost_per_unit()
        if shares is None:
            return None
        return (Decimal(line.unit_price or 0) + shares.get(line.id, ZERO), line.purchase.currency)

    @property
    def label(self):
        """How a vehicle is named in a list: its reference, and its chassis
        number once known."""
        return "%s · %s" % (self.ref, self.chassis_number) if self.chassis_number else (self.ref or "—")


# ── Selling ──────────────────────────────────────────────────────────────────


class Customer(db.Model):
    """Who buys. Names are not unique: two customers may share one."""
    __tablename__ = "customers"

    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(120), nullable=False, index=True)
    phone      = db.Column(db.String(60))
    email      = db.Column(db.String(120))
    address    = db.Column(db.String(255))
    tax_id     = db.Column(db.String(60))                 # NIF, for a company buyer's invoices
    note       = db.Column(db.Text)
    is_active  = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    sales = db.relationship("Sale", back_populates="customer", order_by="Sale.sale_date.desc()")

    @property
    def has_history(self):
        return bool(self.sales)


SALE_STATUSES = ("preorder", "confirmed", "delivered", "cancelled")


class Sale(db.Model):
    """A sale to a customer, numbered by the app (VT-2026-001). It may be
    taken before the vehicle exists in stock -- a pre-order -- and the unit
    named on its line later."""
    __tablename__ = "sales"

    id           = db.Column(db.Integer, primary_key=True)
    number       = db.Column(db.String(20), nullable=False, unique=True)
    customer_id  = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False, index=True)
    sale_date    = db.Column(db.Date, nullable=False)
    currency     = db.Column(db.String(3), nullable=False, default="USD")
    status       = db.Column(db.String(12), nullable=False, default="preorder", index=True)
    delivered_on = db.Column(db.Date)
    note         = db.Column(db.Text)
    created_by   = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    created_at   = db.Column(db.DateTime, nullable=False, default=utcnow)

    customer = db.relationship("Customer", back_populates="sales")
    lines    = db.relationship("SaleLine", back_populates="sale",
                               cascade="all, delete-orphan", order_by="SaleLine.id")
    payments = db.relationship("Payment", back_populates="sale", order_by="Payment.paid_on")
    invoices = db.relationship("Invoice", back_populates="sale", order_by="Invoice.id")

    @property
    def invoice(self):
        """The invoice in force: the latest one not cancelled, if any."""
        live = [i for i in self.invoices if i.status != "cancelled"]
        return live[-1] if live else None

    @property
    def total(self):
        return sum((Decimal(l.price or 0) for l in self.lines), ZERO)

    @property
    def paid_amount(self):
        """Receipts, in the sale's currency: a USD sale may be paid partly in
        GNF, each receipt converted at the rate of its own day."""
        return sum((p.amount_in(self.currency) or ZERO
                    for p in self.payments if not p.is_voided), ZERO)

    @property
    def balance(self):
        if self.status == "cancelled":
            return ZERO
        return max(self.total - self.paid_amount, ZERO)

    @property
    def pay_status(self):
        if self.status == "cancelled":
            return "cancelled"
        if self.total > 0 and self.balance <= 0:
            return "paid"
        return "partial" if self.paid_amount > 0 else "unpaid"


class Invoice(db.Model):
    """A sale's invoice, numbered by the app (FAC-2026-001).

    What it prints is copied when it is issued -- the customer, the lines,
    the prices, the tax -- so a paper already handed over never changes when
    the sale is edited later. To correct one it is cancelled, never deleted:
    it keeps its number and the reason, and a new one is issued.

    Prices are what the customer pays (TTC). With VAT on, the tax is the part
    of that price above the net: vat = total x rate / (100 + rate)."""
    __tablename__ = "invoices"

    id            = db.Column(db.Integer, primary_key=True)
    number        = db.Column(db.String(20), nullable=False, unique=True)
    sale_id       = db.Column(db.Integer, db.ForeignKey("sales.id"), nullable=False, index=True)
    issued_on     = db.Column(db.Date, nullable=False)
    due_on        = db.Column(db.Date)
    currency      = db.Column(db.String(3), nullable=False)
    total         = db.Column(MONEY, nullable=False)          # what the customer pays, tax included
    vat_rate      = db.Column(db.Numeric(5, 2))               # None: no VAT on this invoice
    vat_amount    = db.Column(MONEY)
    customer_name    = db.Column(db.String(120), nullable=False)
    customer_address = db.Column(db.String(255))
    customer_phone   = db.Column(db.String(60))
    customer_tax_id  = db.Column(db.String(60))
    lines         = db.Column(db.JSON, nullable=False)        # [{ref, product, description, chassis, price}]
    note          = db.Column(db.Text)
    status        = db.Column(db.String(12), nullable=False, default="issued")   # issued | cancelled
    cancelled_at  = db.Column(db.DateTime)
    cancel_reason = db.Column(db.String(255))
    created_by    = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    created_at    = db.Column(db.DateTime, nullable=False, default=utcnow)

    sale    = db.relationship("Sale", back_populates="invoices")
    creator = db.relationship("User")

    @property
    def is_cancelled(self):
        return self.status == "cancelled"

    @property
    def net_amount(self):
        return Decimal(self.total) - Decimal(self.vat_amount or 0)


def vat_part(total, rate):
    """The VAT inside a price that includes it, to the cent."""
    if rate is None:
        return None
    return (Decimal(total) * Decimal(rate) / (Decimal(100) + Decimal(rate))).quantize(CENT, rounding=ROUND_HALF_UP)


class SaleLine(db.Model):
    """One vehicle sold: a model at an agreed price, and the unit once one
    is assigned."""
    __tablename__ = "sale_lines"

    id          = db.Column(db.Integer, primary_key=True)
    sale_id     = db.Column(db.Integer, db.ForeignKey("sales.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id  = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)
    unit_id     = db.Column(db.Integer, db.ForeignKey("units.id", ondelete="SET NULL"), index=True)
    price       = db.Column(MONEY, nullable=False, default=0)     # in the sale's currency
    description = db.Column(db.String(255))

    sale    = db.relationship("Sale", back_populates="lines")
    product = db.relationship("Product")
    unit    = db.relationship("Unit", back_populates="sale_lines")


# ── Money ────────────────────────────────────────────────────────────────────

PAYMENT_METHODS = ("cash", "bank", "mobile_money", "cheque", "other")


def next_number(column, prefix):
    """The next number in a yearly series: VT-2026-004 after VT-2026-003.

    Read off the numbers in use; the column's unique constraint catches a
    tie, and the caller retries. Numbers are never reused: a cancelled sale
    or a voided receipt keeps its number."""
    highest = 0
    with db.session.no_autoflush:
        for (n,) in db.session.query(column).filter(column.like(prefix + "%")):
            try:
                highest = max(highest, int(n[len(prefix):]))
            except (TypeError, ValueError):
                pass
    return "%s%03d" % (prefix, highest + 1)


class Payment(db.Model):
    """Money that moved, once.

    `in`: received from a customer against a sale. Numbered REC-2026-001 by
    the app, which is the receipt handed over.
    `out`: paid to a supplier against a purchase, or against one of its extra
    costs (customs, port...). Carries the payee's own reference instead.

    A payment entered by mistake may be corrected or deleted only within the
    correction window of the settings; past it, it is voided, never deleted:
    a receipt number may already be on paper in someone's hands.
    """
    __tablename__ = "payments"
    __table_args__ = (
        db.CheckConstraint(
            "(direction = 'in' AND sale_id IS NOT NULL AND purchase_id IS NULL AND purchase_cost_id IS NULL)"
            " OR (direction = 'out' AND sale_id IS NULL AND"
            " ((purchase_id IS NOT NULL AND purchase_cost_id IS NULL) OR"
            " (purchase_id IS NULL AND purchase_cost_id IS NOT NULL)))",
            name="ck_payment_target"),
    )

    id               = db.Column(db.Integer, primary_key=True)
    direction        = db.Column(db.String(3), nullable=False)               # in | out
    sale_id          = db.Column(db.Integer, db.ForeignKey("sales.id"), index=True)
    purchase_id      = db.Column(db.Integer, db.ForeignKey("purchases.id"), index=True)
    purchase_cost_id = db.Column(db.Integer, db.ForeignKey("purchase_costs.id"), index=True)
    receipt_number   = db.Column(db.String(20), unique=True)                 # REC-2026-001, money in only
    paid_on          = db.Column(db.Date, nullable=False, index=True)
    amount           = db.Column(MONEY, nullable=False)
    currency         = db.Column(db.String(3), nullable=False)
    rate             = db.Column(RATE)                                       # GNF per USD on that day
    method           = db.Column(db.String(20), nullable=False, default="cash")
    reference        = db.Column(db.String(60))                              # transfer / cheque no.
    note             = db.Column(db.String(255))
    voided_at        = db.Column(db.DateTime)
    void_reason      = db.Column(db.String(255))
    created_by       = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    created_at       = db.Column(db.DateTime, nullable=False, default=utcnow)

    sale          = db.relationship("Sale", back_populates="payments")
    purchase      = db.relationship("Purchase", back_populates="payments")
    purchase_cost = db.relationship("PurchaseCost", back_populates="payments")
    creator       = db.relationship("User", foreign_keys=[created_by])

    @property
    def is_voided(self):
        return self.voided_at is not None

    def amount_in(self, currency):
        return convert(self.amount, self.currency, currency, self.rate)
