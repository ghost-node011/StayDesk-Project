# Hostel & PG Management System

Flask + PostgreSQL (Supabase). Room map, admissions, monthly rent with proration,
automatic late fees, receipts, checkout with deposit settlement, complaints,
notices and a resident portal.

## Files
| File | What it does |
|---|---|
| `app.py` | Flask routes, login, CSRF, template filters |
| `db.py` | Reads `DATABASE_URL`, hands out pooled psycopg 3 connections (dict rows) |
| `billing.py` | Money rules only: proration, due dates, late fees, settlement |
| `services.py` | DB workflows: monthly bills, late-fee refresh, payments, checkout |
| `schema.sql` | PostgreSQL schema (idempotent) + `bill_status` view + RLS on |
| `init_db.py` | Creates tables and loads sample data; safe to run twice |
| `tests/` | `smoke_test.py` (every page), `flow_test.py` (admit→pay→checkout), `check_billing.py` (amounts) |

## Rules
- Rent is due on the 10th (or 7 days after admission for someone who joins after the 10th).
- First and last months are prorated by days stayed, rounded to the rupee.
- Late fee: ₹50/day after the due date while money is owed, max ₹500 per bill.
- Checkout: unpaid bills are cleared from the deposit; refund = deposit − dues − damages.

## Run locally
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # then fill DATABASE_URL and SECRET_KEY
.venv/bin/python init_db.py   # create tables + sample data (once)
.venv/bin/flask --app app run --debug   # http://127.0.0.1:5000
```
`python init_db.py --reset` wipes and reseeds the demo.

## Deploy on Render
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn app:app --workers 2 --threads 4 --timeout 60`
- Environment: `DATABASE_URL` (Supabase **Session pooler**, port 5432), `SECRET_KEY` (long random string), `PYTHON_VERSION=3.12.7`
- Pick the **Singapore** region (closest to the Supabase Tokyo database).

Serverless hosts (Vercel etc.): use the Supabase **Transaction pooler** (port 6543).
`db.py` detects port 6543 and sets `prepare_threshold=None` automatically.

## Demo logins
- Warden: `warden@hostel.local` / `warden123`
- Residents: `<firstname>@hostel.local` / `resident123` (e.g. `rahul@hostel.local`)
