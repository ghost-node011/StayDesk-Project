"""Log in as warden and as a resident and load every page. Read-only except the session."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import app  # noqa: E402

def login(client, email, password):
    page = client.get("/login").get_data(as_text=True)
    token = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = client.post("/login", data={"csrf": token, "email": email, "password": password})
    assert r.status_code == 302, f"login failed for {email}"

def check(client, path, expect=200):
    r = client.get(path)
    ok = r.status_code == expect
    print(f"{'OK ' if ok else 'BAD'} {r.status_code} {path}")
    return ok

ok = True
with app.test_client() as c:
    login(c, "warden@hostel.local", "warden123")
    for p in ["/dashboard", "/rooms", "/rooms?block=Girls", "/residents", "/residents?status=checked_out",
              "/residents?q=101", "/residents/new", "/rent", "/rent?status=overdue", "/complaints",
              "/complaints?status=all", "/notices", "/account/password", "/healthz"]:
        ok &= check(c, p)
    from db import connection
    with connection() as db:
        rid = db.execute("SELECT id FROM residents WHERE status='active' ORDER BY id LIMIT 1").fetchone()["id"]
        gone = db.execute("SELECT id FROM residents WHERE status='checked_out' LIMIT 1").fetchone()["id"]
        bill = db.execute("SELECT id FROM bill_status WHERE balance > 0 LIMIT 1").fetchone()["id"]
        pay = db.execute("SELECT id FROM payments LIMIT 1").fetchone()["id"]
        room = db.execute("SELECT id FROM rooms LIMIT 1").fetchone()["id"]
        rahul_pay = db.execute("""SELECT p.id FROM payments p JOIN bills b ON b.id=p.bill_id JOIN residents r ON r.id=b.resident_id
                                  WHERE r.email='rahul@hostel.local' LIMIT 1""").fetchone()["id"]
    for p in [f"/residents/{rid}", f"/residents/{gone}", f"/residents/{rid}/edit", f"/residents/{rid}/checkout",
              f"/residents/{rid}/checkout?checkout_date=2026-12-05&damages=300", f"/bills/{bill}/pay",
              f"/receipts/{pay}", f"/rooms/{room}/edit"]:
        ok &= check(c, p)
    ok &= check(c, "/portal", 403)

with app.test_client() as c:
    login(c, "rahul@hostel.local", "resident123")
    ok &= check(c, "/portal")
    ok &= check(c, f"/receipts/{rahul_pay}")
    ok &= check(c, f"/receipts/{pay}", 404 if pay != rahul_pay else 200)
    ok &= check(c, "/dashboard", 403)
    ok &= check(c, "/rent", 403)

with app.test_client() as c:
    ok &= check(c, "/dashboard", 302)
    ok &= check(c, "/nope", 404)

print("\nALL PAGES OK" if ok else "\nSOME PAGES FAILED")
sys.exit(0 if ok else 1)
