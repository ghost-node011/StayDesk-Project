"""Database workflows: monthly billing, late fees, payments and checkout settlement.

Every function takes an open psycopg connection; the caller decides when to commit.
"""
import datetime as dt
from decimal import Decimal

import billing


class ServiceError(Exception):
    """A rule was broken; the message is safe to show to the user."""


# ---------------------------------------------------------------- billing ---

def create_bill(db, resident, month):
    """Insert the bill for one resident and month (no-op if it already exists)."""
    amount, days, dim = billing.prorated_rent(
        resident["monthly_rent"], month, resident["admitted_on"], resident.get("checked_out_on"))
    if days == 0:
        return None
    row = db.execute(
        """INSERT INTO bills (resident_id, month, monthly_rent, days_billed, days_in_month, amount, due_date)
           VALUES (%s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (resident_id, month) DO NOTHING
           RETURNING id""",
        (resident["id"], month, resident["monthly_rent"], days, dim, amount,
         billing.due_date(month, resident["admitted_on"])),
    ).fetchone()
    return row["id"] if row else None


def generate_bills(db, month):
    """Create the missing bills for every active resident for `month`. Returns how many were created."""
    month = billing.month_start(month)
    residents = db.execute(
        """SELECT r.id, r.monthly_rent, r.admitted_on, r.checked_out_on
           FROM residents r
           WHERE r.status = 'active' AND r.admitted_on <= %s
             AND NOT EXISTS (SELECT 1 FROM bills b WHERE b.resident_id = r.id AND b.month = %s)""",
        (billing.month_end(month), month),
    ).fetchall()
    return sum(1 for r in residents if create_bill(db, r, month))


def refresh_late_fees(db, today):
    """Bring late fees up to date on every overdue bill that still has money owed."""
    cur = db.execute(
        """UPDATE bills b
              SET late_fee = LEAST(%(cap)s, %(rate)s * (%(today)s::date - b.due_date))
             FROM bill_status s
            WHERE s.id = b.id
              AND s.balance > 0
              AND b.due_date < %(today)s::date
              AND b.late_fee < LEAST(%(cap)s, %(rate)s * (%(today)s::date - b.due_date))""",
        {"cap": billing.LATE_FEE_CAP, "rate": billing.LATE_FEE_PER_DAY, "today": today},
    )
    return cur.rowcount


def housekeeping(db, today):
    generate_bills(db, billing.month_start(today))
    refresh_late_fees(db, today)


# --------------------------------------------------------------- payments ---

METHODS = ("UPI", "Cash", "Bank transfer", "Card")


def record_payment(db, bill_id, amount, paid_on, method, reference=None):
    """Record a payment against a bill and return (payment_id, receipt_no)."""
    if method not in METHODS:
        raise ServiceError("Choose a valid payment method.")
    if amount <= 0:
        raise ServiceError("Amount must be more than zero.")
    db.execute("SELECT id FROM bills WHERE id = %s FOR UPDATE", (bill_id,))
    bill = db.execute("SELECT balance FROM bill_status WHERE id = %s", (bill_id,)).fetchone()
    if bill is None:
        raise ServiceError("Bill not found.")
    if amount > bill["balance"]:
        raise ServiceError(f"Amount is more than the balance due (₹{bill['balance']:,.0f}).")
    row = db.execute(
        """INSERT INTO payments (bill_id, amount, paid_on, method, reference)
           VALUES (%s, %s, %s, %s, %s) RETURNING id, receipt_no""",
        (bill_id, amount, paid_on, method, reference or None),
    ).fetchone()
    return row["id"], row["receipt_no"]


# --------------------------------------------------------------- checkout ---

def checkout_plan(db, resident, checkout_date, damages):
    """Work out what changes at checkout without writing anything.

    Returns a dict with per-bill adjustments, total dues and the refund.
    """
    if checkout_date < resident["admitted_on"]:
        raise ServiceError("Checkout date cannot be before the admission date.")
    if damages < 0:
        raise ServiceError("Damages cannot be negative.")
    final_month = billing.month_start(checkout_date)
    bills = db.execute(
        "SELECT * FROM bill_status WHERE resident_id = %s ORDER BY month", (resident["id"],)
    ).fetchall()

    lines = []
    have_final = False
    for b in bills:
        new_amount, days = b["amount"], b["days_billed"]
        action = "keep"
        if b["month"] == final_month:
            have_final = True
            new_amount, days, _ = billing.prorated_rent(
                resident["monthly_rent"], b["month"], resident["admitted_on"], checkout_date)
            action = "prorate" if new_amount != b["amount"] else "keep"
        elif b["month"] > final_month:
            new_amount, days = Decimal("0"), 0
            action = "cancel" if b["paid"] == 0 else "zero"
        lines.append({**b, "new_amount": new_amount, "new_days": days, "action": action,
                      "new_balance": new_amount + b["late_fee"] - b["paid"]})

    if not have_final:
        amount, days, dim = billing.prorated_rent(
            resident["monthly_rent"], final_month, resident["admitted_on"], checkout_date)
        if days:
            lines.append({"id": None, "month": final_month, "amount": Decimal("0"), "late_fee": Decimal("0"),
                          "paid": Decimal("0"), "days_in_month": dim, "new_amount": amount, "new_days": days,
                          "action": "add", "new_balance": amount,
                          "due_date": billing.due_date(final_month, resident["admitted_on"])})
        lines.sort(key=lambda l: l["month"])

    dues = sum((l["new_balance"] for l in lines if l["action"] != "cancel"), Decimal("0"))
    return {
        "lines": lines,
        "dues": dues,
        "deposit": resident["deposit"],
        "damages": damages,
        "refund": billing.settlement(resident["deposit"], dues, damages),
        "checkout_date": checkout_date,
    }


def checkout(db, resident, checkout_date, damages, damage_note=None):
    """Settle the deposit and check the resident out. Returns the plan that was applied."""
    if resident["status"] != "active":
        raise ServiceError("This resident has already checked out.")
    plan = checkout_plan(db, resident, checkout_date, damages)

    for l in plan["lines"]:
        if l["action"] in ("prorate", "zero"):
            db.execute("UPDATE bills SET amount = %s, days_billed = %s WHERE id = %s",
                       (l["new_amount"], l["new_days"], l["id"]))
        elif l["action"] == "cancel":
            db.execute("DELETE FROM bills WHERE id = %s", (l["id"],))
        elif l["action"] == "add":
            l["id"] = db.execute(
                """INSERT INTO bills (resident_id, month, monthly_rent, days_billed, days_in_month, amount, due_date)
                   VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (resident["id"], l["month"], resident["monthly_rent"], l["new_days"], l["days_in_month"],
                 l["new_amount"], l["due_date"]),
            ).fetchone()["id"]

    # Use the deposit to clear unpaid bills, oldest first.
    available = Decimal(resident["deposit"])
    for l in plan["lines"]:
        if l["action"] == "cancel" or l["new_balance"] <= 0 or available <= 0:
            continue
        take = min(l["new_balance"], available)
        db.execute(
            """INSERT INTO payments (bill_id, amount, paid_on, method, reference)
               VALUES (%s, %s, %s, 'Deposit adjustment', 'Adjusted from security deposit')""",
            (l["id"], take, checkout_date),
        )
        available -= take

    db.execute(
        """INSERT INTO settlements (resident_id, checkout_date, deposit, dues, damages, damage_note, refund)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (resident["id"], checkout_date, plan["deposit"], plan["dues"], plan["damages"],
         damage_note or None, plan["refund"]),
    )
    db.execute("UPDATE residents SET status = 'checked_out', checked_out_on = %s WHERE id = %s",
               (checkout_date, resident["id"]))
    return plan


# ------------------------------------------------------------------ rooms ---

def free_beds(db, room_id, lock=False):
    room = db.execute(
        "SELECT * FROM rooms WHERE id = %s" + (" FOR UPDATE" if lock else ""), (room_id,)
    ).fetchone()
    if room is None:
        raise ServiceError("Room not found.")
    taken = {r["bed_no"] for r in db.execute(
        "SELECT bed_no FROM residents WHERE room_id = %s AND status = 'active'", (room_id,))}
    return room, [n for n in range(1, room["beds"] + 1) if n not in taken]


def today_ist():
    from zoneinfo import ZoneInfo
    return dt.datetime.now(ZoneInfo("Asia/Kolkata")).date()
