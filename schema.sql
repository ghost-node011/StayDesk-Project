-- Hostel & PG Management System — PostgreSQL schema (Supabase)
-- Safe to run repeatedly: every statement is IF NOT EXISTS / OR REPLACE.

CREATE TABLE IF NOT EXISTS app_meta (
    key   VARCHAR(40) PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rooms (
    id            SERIAL PRIMARY KEY,
    number        VARCHAR(10)   NOT NULL UNIQUE,
    block         VARCHAR(10)   NOT NULL CHECK (block IN ('Boys', 'Girls')),
    floor         SMALLINT      NOT NULL CHECK (floor BETWEEN 0 AND 20),
    beds          SMALLINT      NOT NULL CHECK (beds BETWEEN 1 AND 6),
    rent_per_bed  NUMERIC(10,2) NOT NULL CHECK (rent_per_bed >= 0),
    is_ac         BOOLEAN       NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS residents (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(100)  NOT NULL,
    gender          VARCHAR(6)    NOT NULL CHECK (gender IN ('Male', 'Female')),
    phone           VARCHAR(15)   NOT NULL,
    email           VARCHAR(120)  NOT NULL UNIQUE,
    guardian_name   VARCHAR(100),
    guardian_phone  VARCHAR(15),
    id_proof        VARCHAR(60),
    institution     VARCHAR(120),
    room_id         INTEGER       NOT NULL REFERENCES rooms(id),
    bed_no          SMALLINT      NOT NULL CHECK (bed_no >= 1),
    monthly_rent    NUMERIC(10,2) NOT NULL CHECK (monthly_rent >= 0),  -- locked at admission
    deposit         NUMERIC(10,2) NOT NULL DEFAULT 0 CHECK (deposit >= 0),
    admitted_on     DATE          NOT NULL,
    checked_out_on  DATE,
    status          VARCHAR(12)   NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'checked_out')),
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (checked_out_on IS NULL OR checked_out_on >= admitted_on)
);
-- A bed can hold only one active resident at a time (enforced by the database).
CREATE UNIQUE INDEX IF NOT EXISTS uq_residents_active_bed
    ON residents (room_id, bed_no) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS users (
    id             SERIAL PRIMARY KEY,
    email          VARCHAR(120) NOT NULL UNIQUE,
    password_hash  TEXT         NOT NULL,
    role           VARCHAR(10)  NOT NULL CHECK (role IN ('warden', 'resident')),
    name           VARCHAR(100) NOT NULL,
    resident_id    INTEGER UNIQUE REFERENCES residents(id) ON DELETE CASCADE,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK ((role = 'resident') = (resident_id IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS bills (
    id             SERIAL PRIMARY KEY,
    resident_id    INTEGER       NOT NULL REFERENCES residents(id) ON DELETE CASCADE,
    month          DATE          NOT NULL CHECK (EXTRACT(DAY FROM month) = 1),
    monthly_rent   NUMERIC(10,2) NOT NULL,
    days_billed    SMALLINT      NOT NULL CHECK (days_billed >= 0),
    days_in_month  SMALLINT      NOT NULL CHECK (days_in_month BETWEEN 28 AND 31),
    amount         NUMERIC(10,2) NOT NULL CHECK (amount >= 0),   -- prorated rent
    late_fee       NUMERIC(10,2) NOT NULL DEFAULT 0 CHECK (late_fee >= 0),
    due_date       DATE          NOT NULL,
    created_at     TIMESTAMPTZ   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (resident_id, month)
);
CREATE INDEX IF NOT EXISTS ix_bills_month ON bills (month);

CREATE SEQUENCE IF NOT EXISTS receipt_seq START 10001;

CREATE TABLE IF NOT EXISTS payments (
    id           SERIAL PRIMARY KEY,
    bill_id      INTEGER       NOT NULL REFERENCES bills(id) ON DELETE RESTRICT,
    amount       NUMERIC(10,2) NOT NULL CHECK (amount > 0),
    paid_on      DATE          NOT NULL,
    method       VARCHAR(20)   NOT NULL CHECK (method IN ('Cash', 'UPI', 'Bank transfer', 'Card', 'Deposit adjustment')),
    reference    VARCHAR(60),
    receipt_no   VARCHAR(20)   NOT NULL UNIQUE DEFAULT ('RCP-' || nextval('receipt_seq')),
    created_at   TIMESTAMPTZ   NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_payments_bill ON payments (bill_id);
CREATE INDEX IF NOT EXISTS ix_payments_paid_on ON payments (paid_on);

CREATE TABLE IF NOT EXISTS complaints (
    id           SERIAL PRIMARY KEY,
    resident_id  INTEGER      NOT NULL REFERENCES residents(id) ON DELETE CASCADE,
    category     VARCHAR(20)  NOT NULL CHECK (category IN ('Electrical', 'Plumbing', 'Furniture', 'Cleaning', 'Wi-Fi', 'Food', 'Other')),
    title        VARCHAR(120) NOT NULL,
    description  TEXT         NOT NULL,
    status       VARCHAR(12)  NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'in_progress', 'resolved')),
    warden_note  TEXT,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_complaints_status ON complaints (status);

CREATE TABLE IF NOT EXISTS notices (
    id          SERIAL PRIMARY KEY,
    title       VARCHAR(140) NOT NULL,
    body        TEXT         NOT NULL,
    audience    VARCHAR(6)   NOT NULL DEFAULT 'All' CHECK (audience IN ('All', 'Boys', 'Girls')),
    pinned      BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS settlements (
    id             SERIAL PRIMARY KEY,
    resident_id    INTEGER       NOT NULL UNIQUE REFERENCES residents(id) ON DELETE CASCADE,
    checkout_date  DATE          NOT NULL,
    deposit        NUMERIC(10,2) NOT NULL,
    dues           NUMERIC(10,2) NOT NULL,   -- unpaid rent + late fees at checkout (negative = credit)
    damages        NUMERIC(10,2) NOT NULL DEFAULT 0 CHECK (damages >= 0),
    damage_note    TEXT,
    refund         NUMERIC(10,2) NOT NULL,   -- positive = refund to resident, negative = still payable
    created_at     TIMESTAMPTZ   NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Balance per bill, used everywhere the app needs "what is still owed".
CREATE OR REPLACE VIEW bill_status WITH (security_invoker = true) AS
SELECT b.id, b.resident_id, b.month, b.monthly_rent, b.days_billed, b.days_in_month,
       b.amount, b.late_fee, b.due_date, b.created_at,
       COALESCE(p.paid, 0)::NUMERIC(10,2)                      AS paid,
       (b.amount + b.late_fee - COALESCE(p.paid, 0))::NUMERIC(10,2) AS balance
FROM bills b
LEFT JOIN (SELECT bill_id, SUM(amount) AS paid FROM payments GROUP BY bill_id) p
       ON p.bill_id = b.id;

-- Supabase exposes the public schema through its REST API. Turn on row level
-- security with no policies so only this app's direct Postgres connection
-- (which bypasses RLS) can read or write these tables.
ALTER TABLE app_meta    ENABLE ROW LEVEL SECURITY;
ALTER TABLE rooms       ENABLE ROW LEVEL SECURITY;
ALTER TABLE residents   ENABLE ROW LEVEL SECURITY;
ALTER TABLE users       ENABLE ROW LEVEL SECURITY;
ALTER TABLE bills       ENABLE ROW LEVEL SECURITY;
ALTER TABLE payments    ENABLE ROW LEVEL SECURITY;
ALTER TABLE complaints  ENABLE ROW LEVEL SECURITY;
ALTER TABLE notices     ENABLE ROW LEVEL SECURITY;
ALTER TABLE settlements ENABLE ROW LEVEL SECURITY;
