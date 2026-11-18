"""End-to-end: admit -> pay -> complaint -> notice -> checkout, then clean up.

Uses a throwaway resident in a free bed; the demo data is left as it was.
"""
import datetime as dt
import re
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import billing  # noqa: E402
import services  # noqa: E402
from app import app  # noqa: E402
from db import connection  # noqa: E402

EMAIL = "flow.test@hostel.local"
today = services.today_ist()


def token(client, path):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).get_data(as_text=True)).group(1)


def login(client, email, pw):
    r = client.post("/login", data={"csrf": token(client, "/login"), "email": email, "password": pw})
    assert r.status_code == 302, "login failed"


def cleanup():
    with connection() as db:
        r = db.execute("SELECT id FROM residents WHERE email = %s", (EMAIL,)).fetchone()
        if r:
            db.execute("DELETE FROM payments WHERE bill_id IN (SELECT id FROM bills WHERE resident_id = %s)", (r["id"],))
            db.execute("DELETE FROM residents WHERE id = %s", (r["id"],))  # cascades bills, user, complaints, settlement
        db.execute("DELETE FROM notices WHERE title = 'Flow test notice'")


def step(msg, cond):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        raise SystemExit(1)


cleanup()
try:
    with connection() as db:
        room = db.execute("""SELECT r.* FROM rooms r WHERE r.block = 'Boys' AND r.beds >
                             (SELECT COUNT(*) FROM residents x WHERE x.room_id = r.id AND x.status = 'active')
                             ORDER BY r.rent_per_bed DESC LIMIT 1""").fetchone()
    admitted = today - dt.timedelta(days=min(9, today.day - 1))
    with app.test_client() as c:
        login(c, "warden@hostel.local", "warden123")
        t = token(c, "/residents/new")
        r = c.post("/residents/new", data={"csrf": t, "name": "Flow Test", "gender": "Male", "phone": "9000000000",
                                           "email": EMAIL, "password": "testpass1", "room_id": room["id"],
                                           "admitted_on": admitted.isoformat(), "deposit": "20000"})
        step(f"admit into room {room['number']} redirects", r.status_code == 302)
        with connection() as db:
            res = db.execute("SELECT * FROM residents WHERE email = %s", (EMAIL,)).fetchone()
            bill = db.execute("SELECT * FROM bill_status WHERE resident_id = %s", (res["id"],)).fetchone()
        exp, days, _ = billing.prorated_rent(room["rent_per_bed"], today, admitted)
        step(f"first bill prorated: {days} days -> ₹{bill['amount']} (expected ₹{exp})", bill["amount"] == exp)

        r = c.post(f"/bills/{bill['id']}/pay", data={"csrf": t, "amount": str(bill["balance"] + 1),
                                                     "paid_on": today.isoformat(), "method": "UPI"})
        step("overpayment is rejected", b"more than the balance" in c.get(r.location if r.status_code == 302 else f"/bills/{bill['id']}/pay").data or r.status_code == 200)
        r = c.post(f"/bills/{bill['id']}/pay", data={"csrf": t, "amount": "3000", "paid_on": today.isoformat(),
                                                     "method": "UPI", "reference": "UPI123"})
        step("partial payment -> receipt page", r.status_code == 302 and "/receipts/" in r.location)
        rec = c.get(r.location).get_data(as_text=True)
        step("receipt shows ₹3,000.00 and receipt number", "₹3,000.00" in rec and "RCP-" in rec)

        with connection() as db:
            other = db.execute("""SELECT r.id FROM rooms r WHERE r.block = 'Boys' AND r.beds >
                                  (SELECT COUNT(*) FROM residents x WHERE x.room_id = r.id AND x.status = 'active')
                                  LIMIT 1""").fetchone()
        r = c.post("/residents/new", data={"csrf": t, "name": "Dup", "gender": "Male", "phone": "9000000001",
                                           "email": EMAIL, "password": "testpass1", "room_id": other["id"],
                                           "admitted_on": today.isoformat(), "deposit": "1"})
        step("duplicate email is rejected", "already used" in r.get_data(as_text=True))
        r = c.post("/residents/new", data={"csrf": "wrong", "name": "x"})
        step("missing/invalid CSRF token -> 400", r.status_code == 400)

        c.post("/notices", data={"csrf": t, "title": "Flow test notice", "body": "hello", "audience": "Boys"})
        step("notice posted", "Flow test notice" in c.get("/notices").get_data(as_text=True))

    with app.test_client() as c:
        login(c, EMAIL, "testpass1")
        page = c.get("/portal").get_data(as_text=True)
        step("new resident portal shows their room and the Boys notice", room["number"] in page and "Flow test notice" in page)
        r = c.post("/portal/complaints", data={"csrf": token(c, "/portal"), "category": "Electrical",
                                              "title": "Fan noisy", "description": "Fan makes noise"})
        step("resident raises complaint", r.status_code == 302)

    with app.test_client() as c:
        login(c, "warden@hostel.local", "warden123")
        t = token(c, "/complaints")
        with connection() as db:
            cid = db.execute("SELECT c.id FROM complaints c JOIN residents r ON r.id = c.resident_id WHERE r.email = %s",
                             (EMAIL,)).fetchone()["id"]
        c.post(f"/complaints/{cid}/update", data={"csrf": t, "status": "resolved", "warden_note": "Fixed"})
        with connection() as db:
            comp = db.execute("SELECT * FROM complaints WHERE id = %s", (cid,)).fetchone()
        step("warden resolves complaint (resolved_at set)", comp["status"] == "resolved" and comp["resolved_at"])

        r = c.post(f"/residents/{res['id']}/checkout", data={"csrf": t, "checkout_date": today.isoformat(),
                                                             "damages": "500", "damage_note": "Mirror"})
        step("checkout succeeds", r.status_code == 302)
        with connection() as db:
            s = db.execute("SELECT * FROM settlements WHERE resident_id = %s", (res["id"],)).fetchone()
            b = db.execute("SELECT * FROM bill_status WHERE resident_id = %s", (res["id"],)).fetchone()
            status = db.execute("SELECT status FROM residents WHERE id = %s", (res["id"],)).fetchone()["status"]
        final, fdays, _ = billing.prorated_rent(room["rent_per_bed"], today, admitted, today)
        dues = final + b["late_fee"] - Decimal("3000")
        step(f"final bill prorated to {fdays} days -> ₹{b['amount']} (expected ₹{final})", b["amount"] == final)
        step(f"refund ₹{s['refund']} = 20000 - dues ₹{dues} - damages 500", s["refund"] == billing.settlement(20000, dues, 500))
        step("bill cleared from deposit, balance 0", b["balance"] == 0)
        step("resident marked checked_out, bed freed", status == "checked_out")
finally:
    cleanup()
    print("cleaned up test data")
