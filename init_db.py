"""Create the tables in Supabase and load the sample hostel.

    python init_db.py           # create tables, seed once (safe to run again)
    python init_db.py --reset   # DROP everything and start over (asks first)
"""
import argparse
import datetime as dt
import sys
from decimal import Decimal
from pathlib import Path

from werkzeug.security import generate_password_hash

import billing
import services
from db import close_pool, connection

WARDEN_EMAIL, WARDEN_PASSWORD = "warden@hostel.local", "warden123"
RESIDENT_PASSWORD = "resident123"

# number, block, floor, beds, rent per bed, AC
ROOMS = [
    ("101", "Boys", 1, 3, 5500, False), ("102", "Boys", 1, 3, 5500, False),
    ("103", "Boys", 1, 2, 7000, False), ("104", "Boys", 1, 1, 9000, False),
    ("201", "Boys", 2, 2, 8500, True), ("202", "Boys", 2, 2, 7000, False),
    ("203", "Boys", 2, 3, 5500, False), ("204", "Boys", 2, 1, 10500, True),
    ("G101", "Girls", 1, 2, 7000, False), ("G102", "Girls", 1, 3, 5500, False),
    ("G103", "Girls", 1, 1, 9000, False), ("G201", "Girls", 2, 2, 8500, True),
    ("G202", "Girls", 2, 2, 7000, False), ("G203", "Girls", 2, 1, 10500, True),
]

# name, room, months before this one they joined, day of month they joined, institution
RESIDENTS = [
    ("Arjun Yadav", "101", 7, 1, "AKTU — KIET Ghaziabad"), ("Kunal Sharma", "101", 5, 3, "AKTU — ABES Engineering College"),
    ("Rohan Mehta", "101", 4, 1, "AKTU — KIET Ghaziabad"), ("Aman Gupta", "102", 6, 1, "AKTU — IMS Engineering College"),
    ("Vivek Tiwari", "102", 3, 1, "AKTU — AKGEC"), ("Harsh Vardhan", "103", 5, 1, "AKTU — ABES Engineering College"),
    ("Siddharth Rao", "103", 4, 1, "AKTU — GL Bajaj"), ("Nikhil Pandey", "104", 8, 1, "Working professional"),
    ("Deepak Verma", "201", 6, 1, "AKTU — KIET Ghaziabad"), ("Rahul Singh", "201", 5, 1, "AKTU — AKGEC"),
    ("Ankit Mishra", "202", 4, 1, "AKTU — GL Bajaj"), ("Prateek Jain", "203", 3, 1, "AKTU — IMS Engineering College"),
    ("Yash Saxena", "203", 3, 1, "AKTU — KIET Ghaziabad"),
    ("Neha Chauhan", "G101", 6, 1, "AKTU — KIET Ghaziabad"), ("Priya Kumari", "G101", 5, 1, "AKTU — AKGEC"),
    ("Ananya Tripathi", "G102", 7, 1, "AKTU — ABES Engineering College"), ("Pooja Rawat", "G102", 1, 12, "AKTU — GL Bajaj"),
    ("Sneha Gupta", "G102", 4, 1, "AKTU — IMS Engineering College"), ("Kavya Srivastava", "G103", 6, 1, "Working professional"),
    ("Ishita Bansal", "G201", 5, 1, "AKTU — KIET Ghaziabad"), ("Riya Agarwal", "G201", 4, 1, "AKTU — AKGEC"),
    ("Simran Kaur", "G202", 0, 5, "AKTU — ABES Engineering College"),
]
GIRLS = {"Neha Chauhan", "Priya Kumari", "Ananya Tripathi", "Pooja Rawat", "Sneha Gupta",
         "Kavya Srivastava", "Ishita Bansal", "Riya Agarwal", "Simran Kaur"}

# Payment story for the demo.  prev = last month, cur = this month.
PREV_UNPAID = {"Ankit Mishra": 0, "Harsh Vardhan": 3300, "Kunal Sharma": 2000, "Pooja Rawat": 0}
CUR_UNPAID = {"Ankit Mishra", "Harsh Vardhan", "Simran Kaur", "Riya Agarwal"}
PAID_LATE_PREV2 = {"Rohan Mehta": 14, "Vivek Tiwari": 13}   # paid on this day, with late fee

COMPLAINTS = [  # resident, category, title, description, status, days ago, warden note
    ("Rahul Singh", "Furniture", "Broken study chair", "One study chair in room 201 has a broken leg.", "open", 1, None),
    ("Sneha Gupta", "Cleaning", "Corridor not mopped", "Corridor outside G102 has not been mopped for two days.", "open", 2, None),
    ("Vivek Tiwari", "Plumbing", "Leaking tap", "The tap in the second bathroom on floor 1 keeps leaking.", "open", 3, None),
    ("Priya Kumari", "Wi-Fi", "Wi-Fi drops at night", "Wi-Fi on the first floor of the girls block drops every evening between 8 and 11 pm.",
     "in_progress", 4, "Technician visiting on Saturday to replace the router."),
    ("Rohan Mehta", "Electrical", "Tube light flickering", "The tube light in room 101 flickers and goes off at night.",
     "resolved", 9, "Tube light and starter replaced."),
    ("Kavya Srivastava", "Food", "Breakfast served late", "Breakfast was served after 8:45 on three days this week.",
     "resolved", 12, "Spoke to the mess contractor; breakfast now starts at 7:30."),
]

NOTICES = [  # title, body, audience, pinned, days ago
    ("Rent due on the 10th", "Monthly rent is due on the 10th. A late fee of ₹50 per day (up to ₹500) applies after that. Pay by UPI at the office or in cash with a receipt.", "All", True, 18),
    ("Water tank cleaning on Sunday", "The overhead tanks will be cleaned on Sunday from 10 am to 2 pm. Please store water the night before.", "All", False, 3),
    ("Girls block: gate timing", "The girls block gate closes at 9:30 pm. Late entry needs a call to the warden in advance.", "Girls", False, 6),
    ("Gym room open again", "The ground floor gym room in the boys block is open again from 6–9 am and 6–9 pm.", "Boys", False, 8),
]


def email_for(name):
    return name.split()[0].lower() + "@hostel.local"


def phone_for(i):
    return f"98{(37145620 + i * 104729) % 100000000:08d}"


def create_schema(db):
    db.execute(Path(__file__).with_name("schema.sql").read_text())


def drop_all(db):
    db.execute("""DROP VIEW IF EXISTS bill_status;
                  DROP TABLE IF EXISTS settlements, notices, complaints, payments, bills, users,
                                       residents, rooms, app_meta CASCADE;
                  DROP SEQUENCE IF EXISTS receipt_seq;""")


def seed(db, today):
    if db.execute("SELECT 1 FROM app_meta WHERE key = 'seeded'").fetchone():
        print("Sample data is already loaded — nothing to do.")
        return False

    cur = billing.month_start(today)
    prev, prev2 = billing.add_months(cur, -1), billing.add_months(cur, -2)

    def on(month, day):
        """A date in `month`, never later than today."""
        return min(today, month.replace(day=min(day, billing.month_end(month).day)))

    room_ids, room_rent = {}, {}
    for number, block, floor, beds, rent, ac in ROOMS:
        row = db.execute(
            """INSERT INTO rooms (number, block, floor, beds, rent_per_bed, is_ac) VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (number) DO UPDATE SET number = EXCLUDED.number RETURNING id""",
            (number, block, floor, beds, rent, ac)).fetchone()
        room_ids[number], room_rent[number] = row["id"], Decimal(rent)

    db.execute(
        """INSERT INTO users (email, password_hash, role, name) VALUES (%s, %s, 'warden', 'Warden')
           ON CONFLICT (email) DO NOTHING""",
        (WARDEN_EMAIL, generate_password_hash(WARDEN_PASSWORD)))

    resident_hash = generate_password_hash(RESIDENT_PASSWORD)
    next_bed, residents = {}, {}
    # Manish stayed in 204 and checked out last month — shows a finished deposit settlement.
    people = RESIDENTS + [("Manish Kumar", "204", 5, 1, "AKTU — AKGEC")]
    for i, (name, room, months_ago, day, inst) in enumerate(people):
        admitted = on(billing.add_months(cur, -months_ago), day) if months_ago == 0 else \
            billing.add_months(cur, -months_ago).replace(day=day)
        bed = next_bed.get(room, 1)
        next_bed[room] = bed + 1
        rent = room_rent[room]
        r = db.execute(
            """INSERT INTO residents (name, gender, phone, email, guardian_name, guardian_phone, id_proof, institution,
                                      room_id, bed_no, monthly_rent, deposit, admitted_on)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (email) DO UPDATE SET email = EXCLUDED.email
               RETURNING *""",
            (name, "Female" if name in GIRLS else "Male", phone_for(i), email_for(name),
             "Mr. " + name.split()[-1], phone_for(i + 50), f"Aadhaar XXXX-XXXX-{4100 + i * 37:04d}", inst,
             room_ids[room], bed, rent, rent * 2, admitted)).fetchone()
        residents[name] = r
        db.execute(
            """INSERT INTO users (email, password_hash, role, name, resident_id) VALUES (%s, %s, 'resident', %s, %s)
               ON CONFLICT (email) DO NOTHING""",
            (r["email"], resident_hash, name, r["id"]))

    def pay(bill_id, amount, paid_on, method):
        if amount > 0:
            db.execute("INSERT INTO payments (bill_id, amount, paid_on, method) VALUES (%s, %s, %s, %s)",
                       (bill_id, amount, paid_on, method))

    methods = ["UPI", "UPI", "Cash", "Bank transfer", "UPI"]
    for i, (name, r) in enumerate(residents.items()):
        method = methods[i % len(methods)]
        for month in (prev2, prev, cur):
            if month == cur and name == "Manish Kumar":
                continue
            services.create_bill(db, r, month)
            bill = db.execute("SELECT * FROM bills WHERE resident_id = %s AND month = %s",
                              (r["id"], month)).fetchone()
            if bill is None:
                continue
            if month == prev2:
                day = PAID_LATE_PREV2.get(name, 5 + i % 4)
                paid_on = on(month, day)
                fee = billing.late_fee(bill["due_date"], paid_on, bill["amount"])
                if fee:
                    db.execute("UPDATE bills SET late_fee = %s WHERE id = %s", (fee, bill["id"]))
                pay(bill["id"], bill["amount"] + fee, paid_on, method)
            elif month == prev:
                if name == "Manish Kumar":
                    continue
                if name in PREV_UNPAID:
                    pay(bill["id"], Decimal(PREV_UNPAID[name]), on(month, 8), method)
                else:
                    pay(bill["id"], bill["amount"], on(month, 4 + i % 6), method)
            elif name not in CUR_UNPAID and today >= bill["due_date"] - dt.timedelta(days=9):
                pay(bill["id"], bill["amount"], min(today, on(month, 3 + i % 6)), method)

    services.refresh_late_fees(db, prev.replace(day=billing.month_end(prev).day))
    manish = db.execute("SELECT * FROM residents WHERE id = %s", (residents["Manish Kumar"]["id"],)).fetchone()
    services.checkout(db, manish, prev.replace(day=20), Decimal("750"), "Cupboard lock replaced; wall repainted near bed.")

    now = dt.datetime.now(dt.timezone.utc)
    for name, cat, title, desc, status, days_ago, note in COMPLAINTS:
        created = now - dt.timedelta(days=days_ago, hours=3)
        db.execute(
            """INSERT INTO complaints (resident_id, category, title, description, status, warden_note,
                                       created_at, updated_at, resolved_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (residents[name]["id"], cat, title, desc, status, note, created,
             created + dt.timedelta(days=1) if status != "open" else created,
             created + dt.timedelta(days=1) if status == "resolved" else None))
    for title, body, audience, pinned, days_ago in NOTICES:
        db.execute("INSERT INTO notices (title, body, audience, pinned, created_at) VALUES (%s, %s, %s, %s, %s)",
                   (title, body, audience, pinned, now - dt.timedelta(days=days_ago)))

    services.refresh_late_fees(db, today)
    db.execute("INSERT INTO app_meta (key, value) VALUES ('seeded', %s)", (today.isoformat(),))
    print(f"Loaded {len(ROOMS)} rooms, {len(people)} residents, bills for "
          f"{prev2:%b}–{cur:%b %Y}, {len(COMPLAINTS)} complaints and {len(NOTICES)} notices.")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="drop all hostel tables first")
    parser.add_argument("--yes", action="store_true", help="skip the --reset confirmation")
    args = parser.parse_args()

    if args.reset and not args.yes:
        if input("This deletes ALL hostel data in the database. Type 'reset' to continue: ").strip() != "reset":
            sys.exit("Cancelled.")

    with connection() as db:   # one transaction: everything or nothing
        if args.reset:
            drop_all(db)
            print("Dropped existing tables.")
        create_schema(db)
        print("Tables are ready.")
        seed(db, services.today_ist())
    close_pool()

    print(f"\nWarden login:   {WARDEN_EMAIL} / {WARDEN_PASSWORD}")
    print(f"Resident login: rahul@hostel.local / {RESIDENT_PASSWORD}  (every resident: <firstname>@hostel.local)")


if __name__ == "__main__":
    main()
