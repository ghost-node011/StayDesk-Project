"""Check every amount stored in PostgreSQL against an independent hand calculation.

The reference here uses plain fractions (no billing.py), so a mistake in either
the rules module or the SQL/NUMERIC round-trip shows up as a mismatch.
"""
import calendar
import sys
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import services  # noqa: E402
from db import close_pool, connection  # noqa: E402


def rupees(fr):  # round half up to whole rupees
    return int(fr + Fraction(1, 2)) if fr >= 0 else -int(-fr + Fraction(1, 2))


def ref_rent(rent, month, admitted, out):
    dim = calendar.monthrange(month.year, month.month)[1]
    first, last = month, month.replace(day=dim)
    start, end = max(first, admitted), min(last, out) if out else last
    days = (end - start).days + 1
    return rupees(Fraction(int(rent)) * days / dim), days


today = services.today_ist()
rows, bad = [], 0
with connection() as db:
    bills = db.execute("""SELECT s.*, r.name, r.admitted_on, r.checked_out_on, r.monthly_rent AS rr
                          FROM bill_status s JOIN residents r ON r.id = s.resident_id ORDER BY r.name, s.month""").fetchall()
    last_pay = {r["bill_id"]: r["d"] for r in db.execute(
        "SELECT bill_id, MAX(paid_on) AS d FROM payments GROUP BY bill_id")}
    settlements = db.execute("SELECT s.*, r.name FROM settlements s JOIN residents r ON r.id = s.resident_id").fetchall()

    for b in bills:
        exp_amt, days = ref_rent(b["rr"], b["month"], b["admitted_on"], b["checked_out_on"])
        # late fee: ₹50/day after due date, max ₹500, frozen once the bill is cleared
        as_of = today if b["balance"] > 0 else last_pay.get(b["id"], b["due_date"])
        exp_fee = min(500, 50 * max(0, (as_of - b["due_date"]).days))
        ok = (b["amount"] == exp_amt and b["days_billed"] == days and b["late_fee"] == exp_fee
              and b["balance"] == b["amount"] + b["late_fee"] - b["paid"])
        bad += not ok
        interesting = b["days_billed"] != b["days_in_month"] or b["late_fee"] > 0
        if interesting or not ok:
            rows.append((b["name"], f"{b['month']:%b %Y}", f"{b['days_billed']}/{b['days_in_month']}",
                         exp_amt, int(b["amount"]), exp_fee, int(b["late_fee"]), "✓" if ok else "✗"))

    for s in settlements:
        exp = rupees(Fraction(s["deposit"]) - Fraction(s["dues"]) - Fraction(s["damages"]))
        ok = s["refund"] == exp
        bad += not ok
        print(f"Settlement {s['name']}: deposit {s['deposit']} - dues {s['dues']} - damages {s['damages']}"
              f" = expected {exp}, stored {s['refund']}  {'✓' if ok else '✗'}")
close_pool()

print(f"\n{'Resident':18} {'Month':9} {'Days':6} {'Rent exp':>9} {'Rent DB':>8} {'Fee exp':>8} {'Fee DB':>7}")
for r in rows:
    print(f"{r[0]:18} {r[1]:9} {r[2]:6} {r[3]:>9} {r[4]:>8} {r[5]:>8} {r[6]:>7}  {r[7]}")
print(f"\nChecked {len(bills)} bills and {len(settlements)} settlement(s): "
      + ("all match." if not bad else f"{bad} MISMATCH(ES)."))
sys.exit(1 if bad else 0)
