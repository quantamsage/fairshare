"""Database connection and schema for FairShare (PostgreSQL)."""
import os
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id       SERIAL PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bills (
    id       SERIAL PRIMARY KEY,
    user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name     TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'KSH',
    created  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Everyone in the bill, including people who paid nothing.
-- weight = how many shares they carry (1 = normal, 0.5 = half, 0 = nothing).
CREATE TABLE IF NOT EXISTS people (
    id      SERIAL PRIMARY KEY,
    bill_id INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    name    TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 60),
    weight  NUMERIC(6, 2) NOT NULL DEFAULT 1 CHECK (weight >= 0 AND weight <= 100)
);
-- One "Amina" per bill, whatever the capitalisation.
CREATE UNIQUE INDEX IF NOT EXISTS people_bill_name ON people (bill_id, lower(name));

CREATE TABLE IF NOT EXISTS payments (
    id          SERIAL PRIMARY KEY,
    bill_id     INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    amount      NUMERIC(12, 2) NOT NULL CHECK (amount > 0)
);
ALTER TABLE payments ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT '';
ALTER TABLE payments ADD COLUMN IF NOT EXISTS created TIMESTAMP DEFAULT CURRENT_TIMESTAMP;

-- Transfers between people that have been marked as paid ("settle up").
CREATE TABLE IF NOT EXISTS settlements (
    id          SERIAL PRIMARY KEY,
    bill_id     INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    from_person INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    to_person   INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    amount      NUMERIC(12, 2) NOT NULL CHECK (amount > 0),
    paid_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CHECK (from_person <> to_person)
);
"""

# Older versions stored the payer's name on each payment. Move those names into
# `people` (one row per distinct name per bill) and link payments to them.
MIGRATE_PAYER_NAMES = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'payments' AND column_name = 'name') THEN
        INSERT INTO people (bill_id, name)
            SELECT DISTINCT ON (bill_id, lower(trim(name))) bill_id, trim(name)
            FROM payments ORDER BY bill_id, lower(trim(name)), id
        ON CONFLICT DO NOTHING;
        ALTER TABLE payments ADD COLUMN IF NOT EXISTS person_id INTEGER REFERENCES people(id) ON DELETE CASCADE;
        UPDATE payments p SET person_id = pe.id
            FROM people pe WHERE pe.bill_id = p.bill_id AND lower(pe.name) = lower(trim(p.name));
        ALTER TABLE payments DROP COLUMN name;
    END IF;
END $$;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS person_id INTEGER REFERENCES people(id) ON DELETE CASCADE;
ALTER TABLE payments ALTER COLUMN person_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS payments_bill ON payments (bill_id);
CREATE INDEX IF NOT EXISTS settlements_bill ON settlements (bill_id);
"""


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env and point it at your "
            "PostgreSQL database, e.g. postgresql://postgres:password@localhost:5432/fairshare"
        )
    return url


@contextmanager
def get_db():
    """Open a connection, commit on success, roll back on error, and always close it."""
    conn = psycopg2.connect(
        database_url(),
        # "prefer" uses SSL when the server offers it (hosted Postgres) and plain
        # connections for a local install. Set DB_SSLMODE=require to insist on SSL.
        sslmode=os.environ.get("DB_SSLMODE", "prefer"),
        cursor_factory=psycopg2.extras.RealDictCursor,
        connect_timeout=10,
    )
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create tables (and upgrade an older database) if needed. Safe to run on every start."""
    with get_db() as conn, conn.cursor() as cur:
        cur.execute(SCHEMA)
        cur.execute(MIGRATE_PAYER_NAMES)
