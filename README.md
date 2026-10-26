# Fels Web

Purchase, customs and resale tracking for a vehicle importer in Guinea.
Tractors, trailers and pickups are bought from suppliers abroad, shipped to
Boffa or Boké, cleared through customs, and sold to local customers, often as
pre-orders paid in instalments, in USD and GNF.

Built on the same Flask + Postgres + Docker stack and design language as
`zone_web`; no domain code is shared with it.

## Status

| Area | State |
|---|---|
| Setup: Docker, Caddy, migrations, seed, backups | Done |
| Design: layout, light/dark, FR/EN, modals, toasts | Done |
| Data model (all tables below) | Done, migrated |
| Login, password change, CSRF, error journal | Done |
| Paramètres: USD→GNF rate, display currency, company details, ports | Done |
| Fournisseurs, Clients, Modèles (catalog) | Done |
| Purchases, pipeline board, customs/port costs, landed cost per vehicle | Done |
| Supplier payments (any currency, own rate), voiding | Done |
| Stock: one row per vehicle, chassis numbers | Done |
| Dropdown filters on every list | Done |
| Sales and pre-orders (VT-YYYY-NNN), vehicle assignment, delivery, cancellation | Done |
| Receipts (REC-YYYY-NNN) in either currency, printable receipt | Done |
| Customer balances and printable statement, margin per vehicle | Done |
| Payments ledger (in and out, filters, totals per currency) | Done |
| Dashboard: receivables, payables, monthly cash chart, margin, stock, alerts | Done |
| Excel export of every list (filters kept) | Done |
| Import of the client's existing workbook, with preview and model matching | Done |
| Ma signature: company stamp + photographed signature on receipts and statements | Done |
| Bell + Alerts page: six alerts computed from the data, thresholds in Paramètres | Done |

## Money rules

- Every amount carries its own currency, USD or GNF. Nothing is converted
  when it is stored.
- The USD→GNF rate lives in Paramètres. It is copied onto each payment and
  cost when entered, so changing it later never rewrites an old figure.
- Totals, amounts paid, balances and statuses are computed from the rows
  beneath them, never typed.
- Extra costs on a purchase (customs duty, port fees, transit, transport) are
  spread over its vehicles in proportion to their price, giving each vehicle
  its landed cost.

## Stack

- **Backend**: Flask 3 + SQLAlchemy 2 + Flask-Migrate (Alembic)
- **Database**: Postgres 16
- **Auth**: Flask-Login, hashed passwords, a CSRF token on every form
- **i18n**: server-side French / English (`languages.py`)
- **Frontend**: server-rendered Jinja, vanilla JS, hand-rolled Material 3 CSS,
  self-hosted Barlow and Material Symbols (subset), "Atelier" theme (slate navigation, orange brand)
- **Container**: Docker Compose (Postgres, gunicorn, Caddy with automatic HTTPS)
- **Backups**: nightly `pg_dump` to Linode Object Storage

## Getting started (local)

```bash
cp .env.example .env     # set SECRET_KEY, DB_PASSWORD (twice), ADMIN_PASSWORD
                         # and SESSION_COOKIE_SECURE=0 for plain-HTTP local use
docker compose up -d --build db web
```

Then open http://127.0.0.1:8002 and sign in with `ADMIN_USERNAME` /
`ADMIN_PASSWORD`. The first boot applies the migrations and seeds the
settings, the two ports and the administrator.

For production, see `OPERATIONS.md`.

## Changing the schema

```bash
# after editing models.py, with the stack running
MSYS_NO_PATHCONV=1 docker compose run --rm --no-deps -v "$(pwd -W 2>/dev/null || pwd):/app" --entrypoint "" \
    web flask db migrate -m "what changed"
```

Review the generated file in `migrations/versions/`, then rebuild: the
entrypoint applies it on boot.

## Project layout

```
fels_web/
├── app.py              # Flask app, shared helpers, auth, dashboard, CLI
├── models.py           # SQLAlchemy models and the money rules
├── languages.py        # FR/EN dictionaries
├── s3_storage.py       # Object storage, for database backups
├── signatures.py       # A photographed signature reduced to transparent ink
├── blueprints/         # one module per area: settings, suppliers, customers, products,
│                       #   purchases (+ pipeline, costs), payments, stock, sales,
│                       #   dashboard, importer
├── exports.py          # Excel export helper
├── templates/          # Jinja templates (_name.html = partial / modal form)
├── static/             # CSS, JS, fonts, images
├── migrations/         # Alembic
├── scripts/            # backup / restore
├── Dockerfile
├── docker-compose.yml
├── Caddyfile
└── entrypoint.sh
```

## Data model

| Table | Holds |
|---|---|
| `users` | The login (one administrator for now) |
| `app_settings` | Rate, display currency, company details |
| `ports` | Arrival ports (Boffa, Boké) |
| `products` | The catalog of models |
| `suppliers` | Who vehicles are bought from, with a default currency |
| `purchases` | One supplier invoice: number, date, port, currency, pipeline stage and the date of each stage |
| `purchase_lines` | Model × quantity × unit price |
| `purchase_costs` | Customs duty, port fees, transit, transport, each in its own currency and rate |
| `units` | One physical vehicle, optional chassis number |
| `customers` | Who buys |
| `sales` | A sale or pre-order, numbered VT-YYYY-NNN |
| `sale_lines` | Model, agreed price, the unit once assigned |
| `payments` | Money in (receipt REC-YYYY-NNN) or out (to a supplier or a cost); voided, never deleted |
| `error_events` | The error journal |

## License

Private / client-licensed.
