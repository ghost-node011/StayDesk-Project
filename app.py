"""Hostel & PG Management System — Flask app backed by Supabase PostgreSQL."""
import datetime as dt
import os
import secrets
from decimal import Decimal, InvalidOperation
from functools import wraps
from zoneinfo import ZoneInfo

import psycopg
from dotenv import load_dotenv
from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import billing
import services
from db import close_db, get_db
from services import ServiceError

load_dotenv()
IST = ZoneInfo("Asia/Kolkata")

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER") or os.environ.get("COOKIE_SECURE")),
    PERMANENT_SESSION_LIFETIME=dt.timedelta(days=7),
)
app.teardown_appcontext(close_db)
HOSTEL_NAME = os.environ.get("APP_NAME", "StayDesk")
CATEGORIES = ("Electrical", "Plumbing", "Furniture", "Cleaning", "Wi-Fi", "Food", "Other")
COMPLAINT_STATUSES = {"open": "Open", "in_progress": "In progress", "resolved": "Resolved"}


# ------------------------------------------------------------- helpers ---

def today():
    return services.today_ist()


def parse_money(raw, label, allow_zero=True):
    try:
        value = Decimal((raw or "").replace(",", "").replace("₹", "").strip())
    except InvalidOperation:
        raise ServiceError(f"{label} must be a number.")
    if not value.is_finite() or value < 0 or (value == 0 and not allow_zero):
        raise ServiceError(f"{label} must be {'0 or more' if allow_zero else 'more than 0'}.")
    if value > Decimal("10000000"):
        raise ServiceError(f"{label} is too large.")
    return value.quantize(Decimal("0.01"))


def parse_date(raw, label):
    try:
        return dt.date.fromisoformat(raw or "")
    except ValueError:
        raise ServiceError(f"{label} must be a valid date.")


def parse_month(raw):
    """'2026-11' -> date(2026, 11, 1); falls back to this month."""
    try:
        y, m = (raw or "").split("-")
        return dt.date(int(y), int(m), 1)
    except (ValueError, TypeError):
        return billing.month_start(today())


def required(form, *fields):
    values = {f: (form.get(f) or "").strip() for f in fields}
    missing = [f.replace("_", " ") for f, v in values.items() if not v]
    if missing:
        raise ServiceError("Please fill in: " + ", ".join(missing) + ".")
    return values


# ---------------------------------------------------------- template glue ---

@app.template_filter("inr")
def inr(value, paise=False):
    value = Decimal(value or 0)
    sign = "−" if value < 0 else ""
    value = abs(value).quantize(Decimal("0.01"))
    whole, frac = divmod(value, 1)
    digits = str(int(whole))
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        digits = ",".join([head] + groups if head else groups) + "," + tail
    cents = f".{int(frac * 100):02d}" if (frac or paise) else ""
    return f"{sign}₹{digits}{cents}"


@app.template_filter("d")
def fmt_date(value, fmt="%d %b %Y"):
    if not value:
        return "—"
    if isinstance(value, dt.datetime):
        value = value.astimezone(IST)
    return value.strftime(fmt)


@app.template_filter("ago")
def ago(value):
    if not value:
        return ""
    secs = (dt.datetime.now(dt.timezone.utc) - value).total_seconds()
    if secs < 3600:
        return f"{max(1, int(secs // 60))} min ago"
    if secs < 86400:
        return f"{int(secs // 3600)} h ago"
    days = int(secs // 86400)
    return "yesterday" if days == 1 else f"{days} days ago" if days < 30 else fmt_date(value)


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


@app.context_processor
def inject_globals():
    return {"hostel_name": HOSTEL_NAME, "today": today(),
            "statuses": COMPLAINT_STATUSES}


def bill_state(bill, on=None):
    """'paid', 'overdue' or 'due' for a bill_status row."""
    if bill["balance"] <= 0:
        return "paid"
    return "overdue" if (on or today()) > bill["due_date"] else "due"


app.jinja_env.globals["bill_state"] = bill_state
app.jinja_env.globals["csrf_token"] = csrf_token


# ------------------------------------------------------------ security ---

@app.before_request
def guard():
    if request.method == "POST":
        token = request.form.get("csrf", "")
        if not token or not secrets.compare_digest(token, session.get("csrf", "")):
            abort(400, "Your form expired. Go back, reload the page and try again.")
    if session.get("user_id"):
        g.user = {"id": session["user_id"], "role": session["role"], "name": session["name"],
                  "email": session["email"], "resident_id": session.get("resident_id")}
        # Create this month's bills and update late fees once a day per worker.
        stamp = today()
        if app.config.get("_housekeeping_done") != stamp:
            services.housekeeping(get_db(), stamp)
            get_db().commit()
            app.config["_housekeeping_done"] = stamp
    else:
        g.user = None


def login_required(role=None):
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if g.user is None:
                return redirect(url_for("login", next=request.full_path if request.method == "GET" else None))
            if role and g.user["role"] != role:
                abort(403)
            return view(*args, **kwargs)
        return wrapper
    return decorator


warden_only = login_required("warden")
resident_only = login_required("resident")


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
def http_error(err):
    titles = {400: "Something was off with that request", 403: "You don't have access to this page",
              404: "Page not found"}
    return render_template("error.html", code=err.code, title=titles.get(err.code, "Error"),
                           message=err.description if err.code == 400 else None), err.code


@app.errorhandler(psycopg.OperationalError)
def db_down(err):
    app.logger.exception("Database connection failed")
    return render_template("error.html", code=503, title="Can't reach the database",
                           message="Check DATABASE_URL and that the Supabase project is running."), 503


# --------------------------------------------------------------- auth ---

@app.route("/")
def index():
    if g.user is None:
        return redirect(url_for("login"))
    return redirect(url_for("dashboard" if g.user["role"] == "warden" else "portal"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        user = get_db().execute("SELECT * FROM users WHERE email = %s", (email,)).fetchone()
        if user and check_password_hash(user["password_hash"], request.form.get("password") or ""):
            session.clear()
            session.permanent = True
            session.update(user_id=user["id"], role=user["role"], name=user["name"], email=user["email"],
                           resident_id=user["resident_id"])
            nxt = request.args.get("next") or ""
            if nxt.startswith("/") and not nxt.startswith("//"):
                return redirect(nxt)
            return redirect(url_for("index"))
        flash("Wrong email or password.", "error")
    return render_template("login.html")


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/account/password", methods=["GET", "POST"])
@login_required()
def change_password():
    if request.method == "POST":
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE id = %s", (g.user["id"],)).fetchone()
        new = request.form.get("new_password") or ""
        if not check_password_hash(user["password_hash"], request.form.get("current_password") or ""):
            flash("Your current password is wrong.", "error")
        elif len(new) < 8:
            flash("The new password must be at least 8 characters.", "error")
        elif new != request.form.get("confirm_password"):
            flash("The two new passwords don't match.", "error")
        else:
            db.execute("UPDATE users SET password_hash = %s WHERE id = %s", (generate_password_hash(new), user["id"]))
            db.commit()
            flash("Password changed.", "success")
            return redirect(url_for("index"))
    return render_template("password.html")


# ---------------------------------------------------------- dashboard ---

@app.route("/dashboard")
@warden_only
def dashboard():
    db = get_db()
    t = today()
    month = billing.month_start(t)
    stats = db.execute(
        """SELECT
             (SELECT COALESCE(SUM(beds), 0) FROM rooms)                                         AS beds,
             (SELECT COUNT(*) FROM residents WHERE status = 'active')                           AS occupied,
             (SELECT COALESCE(SUM(amount), 0) FROM payments
               WHERE paid_on >= %(m)s AND paid_on < %(m)s + INTERVAL '1 month'
                 AND method <> 'Deposit adjustment')                                            AS collected,
             (SELECT COALESCE(SUM(balance), 0) FROM bill_status WHERE balance > 0)              AS pending,
             (SELECT COUNT(*) FROM bill_status WHERE balance > 0)                               AS pending_bills,
             (SELECT COUNT(*) FROM bill_status WHERE balance > 0 AND due_date < %(t)s)          AS overdue_bills,
             (SELECT COUNT(*) FROM complaints WHERE status <> 'resolved')                       AS open_complaints""",
        {"m": month, "t": t}).fetchone()
    blocks = db.execute(
        """SELECT r.block, SUM(r.beds) AS beds,
                  SUM((SELECT COUNT(*) FROM residents x WHERE x.room_id = r.id AND x.status = 'active')) AS occupied
           FROM rooms r GROUP BY r.block ORDER BY r.block""").fetchall()
    pending = db.execute(
        """SELECT s.*, r.name, r.id AS rid, rm.number AS room
           FROM bill_status s JOIN residents r ON r.id = s.resident_id JOIN rooms rm ON rm.id = r.room_id
           WHERE s.balance > 0 ORDER BY s.due_date, r.name LIMIT 8""").fetchall()
    complaints = db.execute(
        """SELECT c.*, r.name, rm.number AS room FROM complaints c
           JOIN residents r ON r.id = c.resident_id JOIN rooms rm ON rm.id = r.room_id
           ORDER BY (c.status = 'resolved'), c.created_at DESC LIMIT 5""").fetchall()
    trend = db.execute(
        """SELECT m::date AS month,
                  COALESCE((SELECT SUM(amount + late_fee) FROM bills WHERE month = m), 0) AS billed,
                  COALESCE((SELECT SUM(amount) FROM payments
                            WHERE paid_on >= m AND paid_on < m + INTERVAL '1 month'
                              AND method <> 'Deposit adjustment'), 0) AS collected
           FROM generate_series(%s::date - INTERVAL '5 months', %s::date, INTERVAL '1 month') AS m""",
        (month, month)).fetchall()
    while len(trend) > 3 and not trend[0]["billed"] and not trend[0]["collected"]:
        trend.pop(0)   # hide empty months before the hostel's first bill
    peak = max([max(r["billed"], r["collected"]) for r in trend] + [Decimal(1)])
    return render_template("dashboard.html", s=stats, blocks=blocks, pending=pending, complaints=complaints,
                           trend=trend, peak=peak, month=month)


# --------------------------------------------------------------- rooms ---

def rooms_with_occupants(db, block=None):
    rooms = db.execute(
        "SELECT * FROM rooms" + (" WHERE block = %s" if block else "") +
        " ORDER BY block, floor, number", (block,) if block else ()).fetchall()
    occupants = db.execute(
        "SELECT id, name, room_id, bed_no FROM residents WHERE status = 'active' ORDER BY bed_no").fetchall()
    by_room = {}
    for o in occupants:
        by_room.setdefault(o["room_id"], {})[o["bed_no"]] = o
    for r in rooms:
        beds = by_room.get(r["id"], {})
        r["bed_list"] = [beds.get(n) for n in range(1, r["beds"] + 1)]
        r["occupied"] = len(beds)
        r["free"] = r["beds"] - len(beds)
    return rooms


@app.route("/rooms")
@warden_only
def rooms():
    block = request.args.get("block")
    block = block if block in ("Boys", "Girls") else None
    rooms = rooms_with_occupants(get_db(), block)
    return render_template("rooms.html", rooms=rooms, block=block,
                           total=sum(r["beds"] for r in rooms), free=sum(r["free"] for r in rooms))


def room_form_values(form):
    v = required(form, "number", "block", "floor", "beds", "rent_per_bed")
    if v["block"] not in ("Boys", "Girls"):
        raise ServiceError("Block must be Boys or Girls.")
    try:
        floor, beds = int(v["floor"]), int(v["beds"])
    except ValueError:
        raise ServiceError("Floor and beds must be whole numbers.")
    if not 0 <= floor <= 20 or not 1 <= beds <= 6:
        raise ServiceError("Floor must be 0–20 and beds 1–6.")
    return {"number": v["number"].upper()[:10], "block": v["block"], "floor": floor, "beds": beds,
            "rent_per_bed": parse_money(v["rent_per_bed"], "Rent per bed", allow_zero=False),
            "is_ac": bool(form.get("is_ac"))}


@app.post("/rooms/add")
@warden_only
def add_room():
    db = get_db()
    try:
        v = room_form_values(request.form)
        db.execute("""INSERT INTO rooms (number, block, floor, beds, rent_per_bed, is_ac)
                      VALUES (%(number)s, %(block)s, %(floor)s, %(beds)s, %(rent_per_bed)s, %(is_ac)s)""", v)
        db.commit()
        flash(f"Room {v['number']} added.", "success")
    except ServiceError as e:
        flash(str(e), "error")
    except psycopg.errors.UniqueViolation:
        db.rollback()
        flash("A room with that number already exists.", "error")
    return redirect(url_for("rooms"))


@app.route("/rooms/<int:room_id>/edit", methods=["GET", "POST"])
@warden_only
def edit_room(room_id):
    db = get_db()
    room = next((r for r in rooms_with_occupants(db) if r["id"] == room_id), None)
    if room is None:
        abort(404)
    if request.method == "POST":
        try:
            v = room_form_values(request.form)
            highest = max([n for n, o in enumerate(room["bed_list"], 1) if o] + [0])
            if v["beds"] < highest:
                raise ServiceError(f"Bed {highest} is occupied, so the room needs at least {highest} beds.")
            db.execute("""UPDATE rooms SET number = %(number)s, block = %(block)s, floor = %(floor)s, beds = %(beds)s,
                          rent_per_bed = %(rent_per_bed)s, is_ac = %(is_ac)s WHERE id = %(id)s""", {**v, "id": room_id})
            db.commit()
            flash(f"Room {v['number']} updated. New admissions pay the new rent; current residents keep theirs.", "success")
            return redirect(url_for("rooms"))
        except ServiceError as e:
            flash(str(e), "error")
        except psycopg.errors.UniqueViolation:
            db.rollback()
            flash("A room with that number already exists.", "error")
    return render_template("room_edit.html", room=room)


@app.post("/rooms/<int:room_id>/delete")
@warden_only
def delete_room(room_id):
    db = get_db()
    used = db.execute("SELECT COUNT(*) AS n FROM residents WHERE room_id = %s", (room_id,)).fetchone()["n"]
    if used:
        flash("This room has resident history, so it can't be deleted. You can edit it instead.", "error")
        return redirect(url_for("edit_room", room_id=room_id))
    db.execute("DELETE FROM rooms WHERE id = %s", (room_id,))
    db.commit()
    flash("Room deleted.", "success")
    return redirect(url_for("rooms"))


# ----------------------------------------------------------- residents ---

@app.route("/residents")
@warden_only
def residents():
    q = (request.args.get("q") or "").strip()
    status = request.args.get("status", "active")
    sql = """SELECT r.*, rm.number AS room, rm.block,
                    COALESCE((SELECT SUM(balance) FROM bill_status s WHERE s.resident_id = r.id AND s.balance > 0), 0) AS due
             FROM residents r JOIN rooms rm ON rm.id = r.room_id WHERE TRUE"""
    params = []
    if status in ("active", "checked_out"):
        sql += " AND r.status = %s"
        params.append(status)
    if q:
        sql += " AND (r.name ILIKE %s OR r.phone ILIKE %s OR rm.number ILIKE %s OR r.email ILIKE %s)"
        params += [f"%{q}%"] * 4
    sql += " ORDER BY rm.block, rm.number, r.bed_no, r.name"
    return render_template("residents.html", residents=get_db().execute(sql, params).fetchall(), q=q, status=status)


@app.route("/residents/new", methods=["GET", "POST"])
@warden_only
def admit():
    db = get_db()
    form = request.form if request.method == "POST" else {}
    if request.method == "POST":
        try:
            v = required(form, "name", "gender", "phone", "email", "room_id", "admitted_on", "deposit", "password")
            if v["gender"] not in ("Male", "Female"):
                raise ServiceError("Choose a gender.")
            if "@" not in v["email"]:
                raise ServiceError("Enter a valid email address.")
            if len(v["password"]) < 8:
                raise ServiceError("Login password must be at least 8 characters.")
            admitted_on = parse_date(v["admitted_on"], "Admission date")
            if admitted_on > today() + dt.timedelta(days=60):
                raise ServiceError("Admission date can be at most 60 days ahead.")
            deposit = parse_money(v["deposit"], "Deposit")
            room, beds = services.free_beds(db, int(v["room_id"]), lock=True)
            if (room["block"] == "Boys") != (v["gender"] == "Male"):
                raise ServiceError(f"Room {room['number']} is in the {room['block'].lower()} block.")
            if not beds:
                raise ServiceError(f"Room {room['number']} is full.")
            r = db.execute(
                """INSERT INTO residents (name, gender, phone, email, guardian_name, guardian_phone, id_proof,
                                          institution, room_id, bed_no, monthly_rent, deposit, admitted_on)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
                (v["name"], v["gender"], v["phone"], v["email"].lower(), form.get("guardian_name") or None,
                 form.get("guardian_phone") or None, form.get("id_proof") or None, form.get("institution") or None,
                 room["id"], beds[0], room["rent_per_bed"], deposit, admitted_on)).fetchone()
            db.execute("""INSERT INTO users (email, password_hash, role, name, resident_id)
                          VALUES (%s, %s, 'resident', %s, %s)""",
                       (r["email"], generate_password_hash(v["password"]), r["name"], r["id"]))
            # Bill every month from admission up to now (usually just one).
            month = billing.month_start(admitted_on)
            while month <= billing.month_start(today()):
                services.create_bill(db, r, month)
                month = billing.add_months(month, 1)
            db.commit()
            flash(f"{r['name']} admitted to room {room['number']}, bed {beds[0]}. They can log in with {r['email']}.",
                  "success")
            return redirect(url_for("resident_detail", resident_id=r["id"]))
        except ServiceError as e:
            db.rollback()
            flash(str(e), "error")
        except psycopg.errors.UniqueViolation:
            db.rollback()
            flash("That email is already used by another resident.", "error")
    rooms = [r for r in rooms_with_occupants(db) if r["free"] > 0]
    return render_template("admit.html", rooms=rooms, form=form,
                           preselect=request.args.get("room", type=int))


def load_resident(db, resident_id):
    r = db.execute(
        """SELECT r.*, rm.number AS room, rm.block, rm.floor, rm.is_ac, u.email AS login
           FROM residents r JOIN rooms rm ON rm.id = r.room_id LEFT JOIN users u ON u.resident_id = r.id
           WHERE r.id = %s""", (resident_id,)).fetchone()
    if r is None:
        abort(404)
    return r


def resident_money(db, resident_id):
    bills = db.execute("SELECT * FROM bill_status WHERE resident_id = %s ORDER BY month DESC",
                       (resident_id,)).fetchall()
    payments = db.execute(
        """SELECT p.*, b.month FROM payments p JOIN bills b ON b.id = p.bill_id
           WHERE b.resident_id = %s ORDER BY p.paid_on DESC, p.id DESC""", (resident_id,)).fetchall()
    due = sum((b["balance"] for b in bills if b["balance"] > 0), Decimal(0))
    return bills, payments, due


@app.route("/residents/<int:resident_id>")
@warden_only
def resident_detail(resident_id):
    db = get_db()
    r = load_resident(db, resident_id)
    bills, payments, due = resident_money(db, resident_id)
    complaints = db.execute("SELECT * FROM complaints WHERE resident_id = %s ORDER BY created_at DESC",
                            (resident_id,)).fetchall()
    settlement = db.execute("SELECT * FROM settlements WHERE resident_id = %s", (resident_id,)).fetchone()
    return render_template("resident_detail.html", r=r, bills=bills, payments=payments, due=due,
                           complaints=complaints, settlement=settlement)


@app.route("/residents/<int:resident_id>/edit", methods=["GET", "POST"])
@warden_only
def edit_resident(resident_id):
    db = get_db()
    r = load_resident(db, resident_id)
    if request.method == "POST":
        try:
            v = required(request.form, "name", "phone")
            db.execute("""UPDATE residents SET name = %s, phone = %s, guardian_name = %s, guardian_phone = %s,
                          id_proof = %s, institution = %s WHERE id = %s""",
                       (v["name"], v["phone"], request.form.get("guardian_name") or None,
                        request.form.get("guardian_phone") or None, request.form.get("id_proof") or None,
                        request.form.get("institution") or None, resident_id))
            db.execute("UPDATE users SET name = %s WHERE resident_id = %s", (v["name"], resident_id))
            if request.form.get("new_password"):
                if len(request.form["new_password"]) < 8:
                    raise ServiceError("Login password must be at least 8 characters.")
                db.execute("UPDATE users SET password_hash = %s WHERE resident_id = %s",
                           (generate_password_hash(request.form["new_password"]), resident_id))
            db.commit()
            flash("Details saved.", "success")
            return redirect(url_for("resident_detail", resident_id=resident_id))
        except ServiceError as e:
            db.rollback()
            flash(str(e), "error")
    return render_template("resident_edit.html", r=r)


@app.route("/residents/<int:resident_id>/checkout", methods=["GET", "POST"])
@warden_only
def checkout(resident_id):
    db = get_db()
    r = load_resident(db, resident_id)
    if r["status"] != "active":
        return redirect(url_for("resident_detail", resident_id=resident_id))
    src = request.form if request.method == "POST" else request.args
    plan, error = None, None
    try:
        checkout_date = parse_date(src.get("checkout_date") or today().isoformat(), "Checkout date")
        if checkout_date > today() + dt.timedelta(days=31):
            raise ServiceError("Checkout date can be at most a month ahead.")
        damages = parse_money(src.get("damages") or "0", "Damages")
        if request.method == "POST":
            services.checkout(db, r, checkout_date, damages, (request.form.get("damage_note") or "").strip())
            db.commit()
            flash(f"{r['name']} checked out. Bed {r['bed_no']} in room {r['room']} is free.", "success")
            return redirect(url_for("resident_detail", resident_id=resident_id))
        plan = services.checkout_plan(db, r, checkout_date, damages)
    except ServiceError as e:
        db.rollback()
        error = str(e)
    return render_template("checkout.html", r=r, plan=plan, error=error,
                           checkout_date=src.get("checkout_date") or today().isoformat(),
                           damages=src.get("damages") or "0", damage_note=src.get("damage_note") or "")


# ---------------------------------------------------------------- rent ---

@app.route("/rent")
@warden_only
def rent():
    db = get_db()
    month = parse_month(request.args.get("month"))
    status = request.args.get("status", "all")
    bills = db.execute(
        """SELECT s.*, r.name, r.id AS rid, r.status AS rstatus, rm.number AS room
           FROM bill_status s JOIN residents r ON r.id = s.resident_id JOIN rooms rm ON rm.id = r.room_id
           WHERE s.month = %s ORDER BY rm.block, rm.number, r.name""", (month,)).fetchall()
    for b in bills:
        b["state"] = bill_state(b)
    totals = {
        "billed": sum((b["amount"] + b["late_fee"] for b in bills), Decimal(0)),
        "paid": sum((b["paid"] for b in bills), Decimal(0)),
        "balance": sum((b["balance"] for b in bills if b["balance"] > 0), Decimal(0)),
        "count": {s: sum(1 for b in bills if b["state"] == s) for s in ("paid", "due", "overdue")},
    }
    if status in ("paid", "due", "overdue"):
        bills = [b for b in bills if b["state"] == status]
    missing = db.execute(
        """SELECT COUNT(*) AS n FROM residents r WHERE r.status = 'active' AND r.admitted_on <= %s
           AND NOT EXISTS (SELECT 1 FROM bills b WHERE b.resident_id = r.id AND b.month = %s)""",
        (billing.month_end(month), month)).fetchone()["n"]
    return render_template("rent.html", bills=bills, month=month, status=status, totals=totals, missing=missing,
                           prev=billing.add_months(month, -1), next=billing.add_months(month, 1),
                           can_generate=month <= billing.add_months(billing.month_start(today()), 1))


@app.post("/rent/generate")
@warden_only
def generate_rent():
    month = parse_month(request.form.get("month"))
    if month > billing.add_months(billing.month_start(today()), 1):
        flash("You can bill at most one month ahead.", "error")
    else:
        db = get_db()
        n = services.generate_bills(db, month)
        db.commit()
        flash(f"Created {n} bill{'s' if n != 1 else ''} for {month:%B %Y}." if n else "Every resident already has a bill for this month.",
              "success")
    return redirect(url_for("rent", month=f"{month:%Y-%m}"))


@app.route("/bills/<int:bill_id>/pay", methods=["GET", "POST"])
@warden_only
def pay_bill(bill_id):
    db = get_db()
    bill = db.execute(
        """SELECT s.*, r.name, r.id AS rid, rm.number AS room FROM bill_status s
           JOIN residents r ON r.id = s.resident_id JOIN rooms rm ON rm.id = r.room_id WHERE s.id = %s""",
        (bill_id,)).fetchone()
    if bill is None:
        abort(404)
    if request.method == "POST":
        try:
            amount = parse_money(request.form.get("amount"), "Amount", allow_zero=False)
            paid_on = parse_date(request.form.get("paid_on"), "Payment date")
            if paid_on > today():
                raise ServiceError("Payment date can't be in the future.")
            pid, receipt_no = services.record_payment(db, bill_id, amount, paid_on, request.form.get("method"),
                                                      (request.form.get("reference") or "").strip()[:60])
            db.commit()
            flash(f"Payment saved. Receipt {receipt_no}.", "success")
            return redirect(url_for("receipt", payment_id=pid))
        except ServiceError as e:
            db.rollback()
            flash(str(e), "error")
    payments = db.execute("SELECT * FROM payments WHERE bill_id = %s ORDER BY paid_on, id", (bill_id,)).fetchall()
    return render_template("pay.html", bill=bill, payments=payments, methods=services.METHODS)


@app.route("/receipts/<int:payment_id>")
@login_required()
def receipt(payment_id):
    db = get_db()
    p = db.execute(
        """SELECT p.*, s.month, s.amount AS bill_amount, s.late_fee, s.paid AS bill_paid, s.balance,
                  s.days_billed, s.days_in_month, s.monthly_rent, s.due_date,
                  r.id AS rid, r.name, r.phone, rm.number AS room, rm.block, r.bed_no
           FROM payments p JOIN bill_status s ON s.id = p.bill_id
           JOIN residents r ON r.id = s.resident_id JOIN rooms rm ON rm.id = r.room_id
           WHERE p.id = %s""", (payment_id,)).fetchone()
    if p is None or (g.user["role"] == "resident" and p["rid"] != g.user["resident_id"]):
        abort(404)
    return render_template("receipt.html", p=p)


# ----------------------------------------------------------- complaints ---

@app.route("/complaints")
@warden_only
def complaints():
    status = request.args.get("status", "active")
    sql = """SELECT c.*, r.name, r.id AS rid, rm.number AS room FROM complaints c
             JOIN residents r ON r.id = c.resident_id JOIN rooms rm ON rm.id = r.room_id"""
    params = ()
    if status == "active":
        sql += " WHERE c.status <> 'resolved'"
    elif status in COMPLAINT_STATUSES:
        sql += " WHERE c.status = %s"
        params = (status,)
    sql += " ORDER BY (c.status = 'resolved'), c.created_at DESC"
    db = get_db()
    counts = {r["status"]: r["n"] for r in db.execute("SELECT status, COUNT(*) AS n FROM complaints GROUP BY status")}
    return render_template("complaints.html", complaints=db.execute(sql, params).fetchall(), status=status,
                           counts=counts)


@app.post("/complaints/<int:complaint_id>/update")
@warden_only
def update_complaint(complaint_id):
    status = request.form.get("status")
    if status not in COMPLAINT_STATUSES:
        abort(400)
    db = get_db()
    db.execute(
        """UPDATE complaints SET status = %s, warden_note = %s, updated_at = CURRENT_TIMESTAMP,
                  resolved_at = CASE WHEN %s = 'resolved' THEN COALESCE(resolved_at, CURRENT_TIMESTAMP) END
           WHERE id = %s""",
        (status, (request.form.get("warden_note") or "").strip() or None, status, complaint_id))
    db.commit()
    flash("Complaint updated.", "success")
    return redirect(request.form.get("back") or url_for("complaints"))


# -------------------------------------------------------------- notices ---

@app.route("/notices", methods=["GET", "POST"])
@warden_only
def notices():
    db = get_db()
    if request.method == "POST":
        try:
            v = required(request.form, "title", "body")
            audience = request.form.get("audience", "All")
            if audience not in ("All", "Boys", "Girls"):
                raise ServiceError("Choose who the notice is for.")
            db.execute("INSERT INTO notices (title, body, audience, pinned) VALUES (%s, %s, %s, %s)",
                       (v["title"][:140], v["body"], audience, bool(request.form.get("pinned"))))
            db.commit()
            flash("Notice posted.", "success")
            return redirect(url_for("notices"))
        except ServiceError as e:
            flash(str(e), "error")
    items = db.execute("SELECT * FROM notices ORDER BY pinned DESC, created_at DESC").fetchall()
    return render_template("notices.html", notices=items)


@app.post("/notices/<int:notice_id>/pin")
@warden_only
def pin_notice(notice_id):
    db = get_db()
    db.execute("UPDATE notices SET pinned = NOT pinned WHERE id = %s", (notice_id,))
    db.commit()
    return redirect(url_for("notices"))


@app.post("/notices/<int:notice_id>/delete")
@warden_only
def delete_notice(notice_id):
    db = get_db()
    db.execute("DELETE FROM notices WHERE id = %s", (notice_id,))
    db.commit()
    flash("Notice deleted.", "success")
    return redirect(url_for("notices"))


# ------------------------------------------------------- resident portal ---

@app.route("/portal")
@resident_only
def portal():
    db = get_db()
    r = load_resident(db, g.user["resident_id"])
    bills, payments, due = resident_money(db, r["id"])
    for b in bills:
        b["state"] = bill_state(b)
    roommates = db.execute(
        """SELECT name, bed_no FROM residents WHERE room_id = %s AND status = 'active' AND id <> %s
           ORDER BY bed_no""", (r["room_id"], r["id"])).fetchall() if r["status"] == "active" else []
    complaints = db.execute("SELECT * FROM complaints WHERE resident_id = %s ORDER BY created_at DESC",
                            (r["id"],)).fetchall()
    notices = db.execute(
        "SELECT * FROM notices WHERE audience IN ('All', %s) ORDER BY pinned DESC, created_at DESC LIMIT 10",
        (r["block"],)).fetchall()
    settlement = db.execute("SELECT * FROM settlements WHERE resident_id = %s", (r["id"],)).fetchone()
    next_due = min((b for b in bills if b["balance"] > 0), key=lambda b: b["due_date"], default=None)
    return render_template("portal.html", r=r, bills=bills, payments=payments, due=due, roommates=roommates,
                           complaints=complaints, notices=notices, settlement=settlement, next_due=next_due,
                           categories=CATEGORIES)


@app.post("/portal/complaints")
@resident_only
def raise_complaint():
    db = get_db()
    try:
        v = required(request.form, "category", "title", "description")
        if v["category"] not in CATEGORIES:
            raise ServiceError("Choose a category.")
        db.execute("INSERT INTO complaints (resident_id, category, title, description) VALUES (%s, %s, %s, %s)",
                   (g.user["resident_id"], v["category"], v["title"][:120], v["description"][:2000]))
        db.commit()
        flash("Complaint sent to the warden.", "success")
    except ServiceError as e:
        flash(str(e), "error")
    return redirect(url_for("portal") + "#complaints")


@app.route("/healthz")
def healthz():
    get_db().execute("SELECT 1")
    return {"ok": True}


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1", port=int(os.environ.get("PORT", 5000)))
