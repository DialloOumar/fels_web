"""Import of the client's Excel workbook (the sheet they kept before the app).

Three steps, because the file is hand-kept and cannot be trusted blindly:

  1. upload   -- the .xlsx is kept aside under a random name, never served;
  2. preview  -- everything the import would create is shown, with what looks
                 wrong (repeated invoice numbers, identical placeholder amounts,
                 paid more than the price...), and each product abbreviation
                 found in the file ("2xFT", "Ttor SHM 400HP") is matched to a
                 model of the catalog -- guessed, then confirmed by hand, or a
                 new model created from it;
  3. confirm  -- all of it is written in one transaction: either everything
                 goes in or nothing does.

Sheets are found by their header row, not their name: a purchases sheet has
"Invoice" and "Description Products"; a sales sheet has "Name" and "Modell".
"""
import os
import re
import secrets
import tempfile
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required

from app import current_rate, get_t
from models import (PRODUCT_CATEGORIES, Customer, Payment, Port, Product,
                    Purchase, PurchaseLine, Sale, SaleLine, Supplier, Unit,
                    db, next_number)

importer_bp = Blueprint("importer", __name__, url_prefix="/parametres/import")

UPLOAD_DIR = os.path.join(tempfile.gettempdir(), "fels_import")
CENT = Decimal("0.01")


# ── Reading the workbook ─────────────────────────────────────────────────────


def _norm(text):
    """Lower case, accents off, spaces collapsed: for comparing names."""
    s = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip().lower()


def _cell_text(v):
    return "" if v is None else str(v).strip()


def parse_money(value):
    """A cell as (amount, currency). Numbers come as they are; text such as
    "10.000 USD", "280.000.000GNF" or "9.950 USD" uses dots as thousands
    separators, the European way. Without a currency written, an amount of a
    million or more is taken as GNF, anything less as USD -- and said so in
    the preview."""
    if value is None or value == "":
        return None, None, False
    if isinstance(value, (int, float, Decimal)):
        amount = Decimal(str(value))
        cur, guessed = ("GNF", True) if amount >= 1_000_000 else ("USD", True)
        return amount.quantize(CENT), cur, guessed
    s = str(value).upper().replace(" ", " ")
    cur = "GNF" if "GNF" in s else ("USD" if ("USD" in s or "$" in s) else None)
    digits = re.sub(r"[^\d,.]", "", s)
    if not digits:
        return None, None, False
    m = re.search(r",(\d{1,2})$", digits)
    whole, frac = (digits[:m.start()], m.group(1)) if m else (digits, "")
    whole = whole.replace(".", "").replace(",", "")
    try:
        amount = Decimal(whole + ("." + frac if frac else "")).quantize(CENT)
    except InvalidOperation:
        return None, None, False
    guessed = cur is None
    if cur is None:
        cur = "GNF" if amount >= 1_000_000 else "USD"
    return amount, cur, guessed


def split_description(text):
    """"2xFence Trailer, 1xTractor SHM430HP" -> [(2, "Fence Trailer"),
    (1, "Tractor SHM430HP")]. "3 Ttor SHM 400HP" counts 3 too. A part
    without a count is one vehicle -- "430HP" is a model, not 430 of them."""
    items = []
    for part in re.split(r"[,;/+]", text or ""):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(\d+)\s*[xX×*]\s*(.+)$", part) or re.match(r"^(\d+)\s+([A-Za-z].*)$", part)
        qty, token = (int(m.group(1)), m.group(2).strip()) if m else (1, part)
        items.append((qty, re.sub(r"\s+", " ", token)))
    return items


def invoice_date(number, year):
    """The supplier's numbers carry their date: TMA260623BR is 23 June 2026.
    Failing that, 1 January of the year column."""
    m = re.match(r"^[A-Za-z]+(\d{2})(\d{2})(\d{2})", number or "")
    if m:
        try:
            return date(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3))), False
        except ValueError:
            pass
    try:
        return date(int(year), 1, 1), True
    except (TypeError, ValueError):
        return date.today(), True


def _find_header(ws, needed):
    """(row index, {header: column index}) of the first row holding all the
    `needed` headers, or (None, None)."""
    for r in range(1, min(ws.max_row, 30) + 1):
        heads = {}
        for c in range(1, ws.max_column + 1):
            v = ws.cell(row=r, column=c).value
            if v is not None:
                heads[_norm(v)] = c
        if all(_norm(n) in heads for n in needed):
            return r, heads
    return None, None


def parse_workbook(path):
    """Everything the file says, as plain data. Nothing is written."""
    from openpyxl import load_workbook
    wb = load_workbook(path, data_only=True, read_only=False)
    out = {"purchases": [], "sales": [], "sheets": [], "legend": {}}
    for ws in wb.worksheets:
        r0, h = _find_header(ws, ["Invoice", "Description Products"])
        if r0:
            out["sheets"].append((ws.title, "purchases"))
            col = lambda name: h.get(_norm(name))
            for r in range(r0 + 1, ws.max_row + 1):
                get = lambda name: ws.cell(row=r, column=col(name)).value if col(name) else None
                number = _cell_text(get("Invoice"))
                if not number:
                    continue
                if not _cell_text(get("Description Products")) and get("Total") in (None, ""):
                    # A row with a name but no goods and no amount: the
                    # legend at the foot of the sheet ("FT | Flatbed Trailers").
                    abbr = re.sub(r"[^A-Za-z0-9]", "", _cell_text(get("Year")))
                    if abbr:
                        out["legend"][abbr.lower()] = number
                    continue
                d, d_guessed = invoice_date(number, get("Year"))
                out["purchases"].append({
                    "sheet": ws.title, "row": r, "number": number, "date": d, "date_guessed": d_guessed,
                    "port": _cell_text(get("Port")),
                    "total": parse_money(get("Total"))[0], "deposit": parse_money(get("Avance"))[0],
                    "balance": parse_money(get("Balance"))[0],
                    "delivered": _norm(get("Delivery")) in ("yes", "oui", "ja", "y"),
                    "fully_paid": "paid" in _norm(get("fully paid")) or "paye" in _norm(get("fully paid")),
                    "description": _cell_text(get("Description Products")),
                    "items": split_description(_cell_text(get("Description Products"))),
                    "comment": _cell_text(get("Comment")),
                    "updated": get("Date") if isinstance(get("Date"), (date, datetime)) else None,
                })
            continue
        r0, h = _find_header(ws, ["Name", "Modell"])
        if r0:
            out["sheets"].append((ws.title, "sales"))
            col = lambda name: h.get(_norm(name))
            for r in range(r0 + 1, ws.max_row + 1):
                get = lambda name: ws.cell(row=r, column=col(name)).value if col(name) else None
                name, model = _cell_text(get("Name")), _cell_text(get("Modell"))
                if not name or not model or _norm(name) in ("gesamt", "total", "totaux"):
                    continue
                price, price_cur, price_guess = parse_money(get("Kaufpreis"))
                paid, paid_cur, paid_guess = parse_money(get("Bezahlt"))
                try:
                    qty = int(get("Anzahl") or 1)
                except (TypeError, ValueError):
                    qty = 1
                out["sales"].append({
                    "sheet": ws.title, "row": r, "name": name, "model": re.sub(r"\s+", " ", model),
                    "qty": max(qty, 1), "price": price, "currency": price_cur, "price_guess": price_guess,
                    "paid": paid or Decimal("0"), "paid_currency": paid_cur or price_cur, "paid_guess": paid_guess and bool(paid),
                    "bank": _norm(get("Konto")) in ("ja", "oui", "yes"),
                    "delivered": _norm(get("Delivered")) in ("yes", "oui", "ja"),
                    "comment": _cell_text(get("comment")),
                })
    return out


# ── Proposals: what would be created, and what looks wrong ───────────────────

# A token is matched to a model by the words it shares with it. Each rule:
# (pattern in the token, words one of which the model's name or code holds).
RULES = [
    (r"fence|fct\b|fct\d|\bfc\b", ("fence",)),
    (r"flat|fbed|fltbed|\bft\b", ("flatbed", "flat")),
    (r"side|\bsw\b", ("sidewall", "side")),
    (r"tank", ("tank",)),
    (r"pick", ("pick", "foton")),
    (r"low ?bed|\blb\b", ("low", "lowbed", "porte-char")),
    (r"430", ("430",)),
    (r"400", ("400",)),
]


def expand(token, legend):
    """The token with the file's own abbreviations spelt out: "2x FT" reads
    as "2x Flatbed Trailers" when the sheet's legend says FT means that."""
    if not legend:
        return token
    return re.sub(r"[A-Za-z]+", lambda m: legend.get(m.group(0).lower(), m.group(0)), token)


def guess_product(token, products, legend=None):
    t = _norm(expand(token, legend))
    for pattern, words in RULES:
        if not re.search(pattern, t):
            continue
        found = [p for p in products if any(w in _norm(p.name + " " + (p.code or "")) for w in words)]
        sizes = re.findall(r"(\d{2})\s*m\b", t)
        if sizes:
            sized = [p for p in found if any(sz in _norm(p.name) for sz in sizes)]
            found = sized or found
        if len(found) >= 1:
            return found[0]
    return None


def guess_category(token, legend=None):
    t = _norm(expand(token, legend))
    if re.search(r"pick", t):
        return "pickup"
    if re.search(r"shm|shacman|tractor|ttor|\b4[03]0", t):
        return "tractor"
    if re.search(r"trailer|tler|fence|flat|side|tank|\bft\b|\bsw\b|\blb\b|bed", t):
        return "trailer"
    return "other"


def build_proposal(data):
    """Group repeated invoice numbers, collect the product tokens, and list
    what looks wrong. Returns a dict for the preview."""
    t = get_t()
    warnings = []
    merged = {}
    for p in data["purchases"]:
        key = p["number"].upper()
        if key in merged:
            m = merged[key]
            m["items"] += p["items"]
            m["description"] += " + " + p["description"]
            m["total"] = (m["total"] or 0) + (p["total"] or 0)
            m["deposit"] = (m["deposit"] or 0) + (p["deposit"] or 0)
            m["rows"].append(p["row"])
            m["warnings"].append(t["import.warn.merged"] % {"rows": ", ".join(map(str, m["rows"]))})
        else:
            merged[key] = dict(p, rows=[p["row"]], warnings=[])
            if p["date_guessed"]:
                merged[key]["warnings"].append(t["import.warn.date_guessed"])
    purchases = list(merged.values())
    for p in purchases:
        if not p["items"]:
            p["warnings"].append(t["import.warn.no_items"])
        if any(re.search(r"\d\s*[xX×]", tok) for _q, tok in p["items"]):
            p["warnings"].append(t["import.warn.ambiguous"])
    # Read on the file's own rows: a merged invoice sums two of them.
    totals = {p["total"] for p in data["purchases"] if p["total"] is not None}
    if len(data["purchases"]) > 2 and len(totals) == 1:
        warnings.append(t["import.warn.same_amounts"] % {"amount": "{:,}".format(int(next(iter(totals)))).replace(",", " ")})

    for s in data["sales"]:
        s["warnings"] = []
        if s["price"] is None:
            s["warnings"].append(t["import.warn.no_price"])
        if s["price_guess"]:
            s["warnings"].append(t["import.warn.currency_guessed"] % {"cur": s["currency"]})
        if s["price"] and s["paid"] and s["paid_currency"] == s["currency"] and s["paid"] > s["price"] * s["qty"]:
            s["warnings"].append(t["import.warn.paid_over"])
        if s["paid"] and s["paid_currency"] != s["currency"]:
            s["warnings"].append(t["import.warn.paid_other_currency"])
        if s["delivered"]:
            s["warnings"].append(t["import.warn.delivered"])

    tokens = []
    seen = set()
    for p in purchases:
        for _q, tok in p["items"]:
            if _norm(tok) not in seen:
                seen.add(_norm(tok)); tokens.append(tok)
    for s in data["sales"]:
        if _norm(s["model"]) not in seen:
            seen.add(_norm(s["model"])); tokens.append(s["model"])
    return {"purchases": purchases, "sales": data["sales"], "tokens": tokens, "warnings": warnings,
            "sheets": data["sheets"], "legend": data.get("legend", {})}


def spread(total, items):
    """[(qty, unit price, token)] whose total is exactly `total`.

    The amount is shared evenly by all the vehicles of the invoice, in cents:
    the first `remainder` vehicles carry one cent more. A line whose vehicles
    fall on both sides of that split becomes two lines, one cent apart, so no
    invoice ends a cent off its paper total."""
    n = sum(q for q, _t in items)
    if n == 0:
        return []
    cents = int((Decimal(total) * 100).quantize(Decimal("1")))
    base, extra = divmod(cents, n)
    out, seen = [], 0
    for qty, tok in items:
        high = max(0, min(qty, extra - seen))      # vehicles of this line carrying base + 1
        if high:
            out.append((high, Decimal(base + 1) / 100, tok))
        if qty - high:
            out.append((qty - high, Decimal(base) / 100, tok))
        seen += qty
    return out


# ── Routes ───────────────────────────────────────────────────────────────────


def _stored_path():
    token = session.get("import_token")
    if not token or not re.fullmatch(r"[A-Za-z0-9_-]{20,}", token):
        return None
    path = os.path.join(UPLOAD_DIR, token + ".xlsx")
    return path if os.path.exists(path) else None


def _forget():
    path = _stored_path()
    if path:
        try:
            os.remove(path)
        except OSError:
            pass
    session.pop("import_token", None)


@importer_bp.route("/", methods=["GET", "POST"])
@login_required
def upload():
    t = get_t()
    error = None
    if request.method == "POST":
        f = request.files.get("file")
        if not f or not f.filename:
            error = t["import.error.no_file"]
        elif not f.filename.lower().endswith(".xlsx"):
            error = t["import.error.type"]
        else:
            head = f.stream.read(4)
            f.stream.seek(0)
            if head != b"PK\x03\x04":              # an .xlsx is a zip
                error = t["import.error.type"]
            else:
                _forget()
                os.makedirs(UPLOAD_DIR, exist_ok=True)
                token = secrets.token_urlsafe(24)
                f.save(os.path.join(UPLOAD_DIR, token + ".xlsx"))
                session["import_token"] = token
                session["import_name"] = os.path.basename(f.filename)[:120]
                return redirect(url_for("importer.preview"))
    return render_template("import_upload.html", error=error)


def _context(proposal):
    products = Product.query.filter_by(is_active=True).order_by(Product.category, Product.name).all()
    legend = proposal.get("legend", {})
    guesses = {tok: guess_product(tok, products, legend) for tok in proposal["tokens"]}
    numbers = [p["number"] for p in proposal["purchases"]]
    prefix = os.path.commonprefix(numbers)[:3] if numbers else ""
    supplier_guess = Supplier.query.filter(Supplier.name.ilike(prefix + "%")).first() if prefix else None
    return {"products": products, "guesses": guesses, "categories": PRODUCT_CATEGORIES,
            "suppliers": Supplier.query.filter_by(is_active=True).order_by(Supplier.name).all(),
            "supplier_guess": supplier_guess, "supplier_name_guess": prefix,
            "guess_category": lambda tok: guess_category(tok, legend), "legend": legend}


@importer_bp.route("/apercu")
@login_required
def preview():
    path = _stored_path()
    if not path:
        flash("error|" + get_t()["import.error.expired"])
        return redirect(url_for("importer.upload"))
    try:
        proposal = build_proposal(parse_workbook(path))
    except Exception:                                   # a file openpyxl cannot read
        _forget()
        flash("error|" + get_t()["import.error.unreadable"])
        return redirect(url_for("importer.upload"))
    return render_template("import_preview.html", p=proposal, filename=session.get("import_name", ""),
                           **_context(proposal))


class ImportError_(Exception):
    pass


@importer_bp.route("/confirmer", methods=["POST"])
@login_required
def confirm():
    t = get_t()
    path = _stored_path()
    if not path:
        flash("error|" + t["import.error.expired"])
        return redirect(url_for("importer.upload"))
    proposal = build_proposal(parse_workbook(path))
    try:
        summary = _apply(proposal)
    except ImportError_ as e:
        db.session.rollback()
        flash("error|" + str(e))
        return redirect(url_for("importer.preview"))
    _forget()
    flash("success|" + t["import.done"] % summary)
    return redirect(url_for("purchases.index"))


def _apply(proposal):
    """Write the proposal with the choices of the form, in one transaction."""
    t = get_t()
    f = request.form
    rate = current_rate()

    # The product of each token: an existing model, or a new one.
    product_of = {}
    for i, tok in enumerate(proposal["tokens"]):
        choice = f.get("map_%d" % i, "")
        if choice == "new":
            name = (f.get("new_name_%d" % i) or tok).strip()[:120]
            cat = f.get("new_cat_%d" % i) or "other"
            existing = Product.query.filter(db.func.lower(Product.name) == name.lower()).first()
            if existing is None:
                existing = Product(name=name, category=cat if cat in PRODUCT_CATEGORIES else "other")
                db.session.add(existing)
                db.session.flush()
            product_of[_norm(tok)] = existing
        elif choice.isdigit() and db.session.get(Product, int(choice)):
            product_of[_norm(tok)] = db.session.get(Product, int(choice))
        else:
            raise ImportError_(t["import.error.unmapped"] % {"token": tok})

    counts = {"purchases": 0, "sales": 0, "customers": 0, "vehicles": 0, "payments": 0, "skipped": 0}
    with_payments = f.get("with_payments") == "1"

    if proposal["purchases"] and f.get("with_purchases") == "1":
        sup_choice = f.get("supplier_id", "")
        if sup_choice == "new":
            name = (f.get("supplier_name") or "").strip()[:120]
            if not name:
                raise ImportError_(t["import.error.supplier"])
            supplier = Supplier.query.filter(db.func.lower(Supplier.name) == name.lower()).first()
            if supplier is None:
                supplier = Supplier(name=name, default_currency="USD", country=(f.get("supplier_country") or "").strip() or None)
                db.session.add(supplier)
                db.session.flush()
        elif sup_choice.isdigit() and db.session.get(Supplier, int(sup_choice)):
            supplier = db.session.get(Supplier, int(sup_choice))
        else:
            raise ImportError_(t["import.error.supplier"])
        ports = {_norm(p.name): p for p in Port.query.all()}
        for row in proposal["purchases"]:
            exists = Purchase.query.filter(Purchase.supplier_id == supplier.id,
                                           db.func.lower(Purchase.invoice_number) == row["number"].lower()).first()
            if exists or not row["items"]:
                counts["skipped"] += 1
                continue
            port = None
            if row["port"]:
                port = ports.get(_norm(row["port"]))
                if port is None:
                    port = Port(name=row["port"].strip()[:60], sort_order=len(ports) + 1)
                    db.session.add(port)
                    db.session.flush()
                    ports[_norm(port.name)] = port
            note_bits = [row["comment"]] if row["comment"] else []
            if row["updated"]:
                note_bits.append(t["import.note.updated"] % {"date": row["updated"].strftime("%d/%m/%Y")})
            note_bits.append(t["import.note.source"] % {"desc": row["description"]})
            purchase = Purchase(supplier=supplier, invoice_number=row["number"][:60], invoice_date=row["date"],
                                port=port, currency="USD", stage="in_stock" if row["delivered"] else "ordered",
                                ordered_on=row["date"], note="\n".join(note_bits), created_by=current_user.id)
            db.session.add(purchase)
            for qty, price, tok in spread(row["total"] or Decimal("0"), row["items"]):
                line = PurchaseLine(product=product_of[_norm(tok)], quantity=qty, unit_price=price,
                                    description=tok[:255])
                purchase.lines.append(line)
                for _ in range(qty):
                    line.units.append(Unit(product=line.product))
                counts["vehicles"] += qty
            if with_payments and row["deposit"]:
                db.session.add(Payment(direction="out", purchase=purchase, paid_on=row["date"], amount=row["deposit"],
                                       currency="USD", rate=rate, method="bank", created_by=current_user.id,
                                       note=t["import.note.deposit"]))
                counts["payments"] += 1
            if with_payments and row["fully_paid"]:
                rest = (row["total"] or 0) - (row["deposit"] or 0)
                if rest > 0:
                    db.session.add(Payment(direction="out", purchase=purchase, paid_on=row["date"], amount=rest,
                                           currency="USD", rate=rate, method="bank", created_by=current_user.id,
                                           note=t["import.note.fully_paid"]))
                    counts["payments"] += 1
            counts["purchases"] += 1

    if proposal["sales"] and f.get("with_sales") == "1":
        today = date.today()
        customers = {}
        for row in proposal["sales"]:
            if row["price"] is None:
                counts["skipped"] += 1
                continue
            key = _norm(row["name"])
            customer = customers.get(key) or Customer.query.filter(db.func.lower(Customer.name) == row["name"].lower()).first()
            if customer is None:
                customer = Customer(name=row["name"][:120])
                db.session.add(customer)
                db.session.flush()
                counts["customers"] += 1
            customers[key] = customer
            note_bits = [row["comment"]] if row["comment"] else []
            if row["delivered"]:
                note_bits.append(t["import.note.delivered"])
            note_bits.append(t["import.note.sheet"] % {"sheet": row["sheet"], "row": row["row"]})
            sale = Sale(customer=customer, sale_date=today, currency=row["currency"], status="confirmed",
                        note="\n".join(note_bits), created_by=current_user.id)
            for _ in range(row["qty"]):
                sale.lines.append(SaleLine(product=product_of[_norm(row["model"])], price=row["price"]))
            db.session.add(sale)
            sale.number = next_number(Sale.number, "VT-%d-" % today.year)
            db.session.flush()
            if with_payments and row["paid"]:
                if row["paid_currency"] != row["currency"] and rate is None:
                    raise ImportError_(t["import.error.rate"])
                pay = Payment(direction="in", sale=sale, paid_on=today, amount=row["paid"], currency=row["paid_currency"],
                              rate=rate, method="bank" if row["bank"] else "cash", created_by=current_user.id,
                              note=t["import.note.receipt"])
                db.session.add(pay)
                pay.receipt_number = next_number(Payment.receipt_number, "REC-%d-" % today.year)
                db.session.flush()
                counts["payments"] += 1
            counts["sales"] += 1

    db.session.commit()
    return counts


@importer_bp.route("/annuler", methods=["POST"])
@login_required
def discard():
    _forget()
    flash("success|" + get_t()["import.discarded"])
    return redirect(url_for("importer.upload"))
