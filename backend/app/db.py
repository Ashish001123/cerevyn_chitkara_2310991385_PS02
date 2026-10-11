"""SQLite schema, connections and seeding."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from .config import TZ

SCHEMA = """
CREATE TABLE IF NOT EXISTS providers (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    practitioner  TEXT NOT NULL,
    service       TEXT NOT NULL UNIQUE,
    keywords      TEXT NOT NULL,          -- JSON array used for service matching
    address       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS slots (
    id           TEXT PRIMARY KEY,
    provider_id  TEXT NOT NULL REFERENCES providers(id),
    start_at     TEXT NOT NULL,           -- ISO-8601 with +05:30 offset
    end_at       TEXT NOT NULL,
    slot_date    TEXT NOT NULL,           -- YYYY-MM-DD in Asia/Kolkata
    slot_time    TEXT NOT NULL,           -- HH:MM in Asia/Kolkata
    status       TEXT NOT NULL DEFAULT 'available' CHECK (status IN ('available', 'booked')),
    UNIQUE (provider_id, start_at)
);

CREATE TABLE IF NOT EXISTS bookings (
    id             TEXT PRIMARY KEY,      -- e.g. BK24017
    slot_id        TEXT NOT NULL REFERENCES slots(id),
    customer_name  TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('confirmed', 'cancelled')),
    session_id     TEXT,
    created_at     TEXT NOT NULL,
    cancelled_at   TEXT
);

-- Defence in depth: at most one confirmed booking per slot, enforced by SQLite.
CREATE UNIQUE INDEX IF NOT EXISTS ux_one_confirmed_booking_per_slot
    ON bookings(slot_id) WHERE status = 'confirmed';

CREATE TABLE IF NOT EXISTS booking_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    action      TEXT NOT NULL,            -- BOOKED | CANCELLED | BOOK_REJECTED_SLOT_TAKEN | SEEDED
    booking_id  TEXT,
    slot_id     TEXT,
    session_id  TEXT,
    actor       TEXT NOT NULL,            -- customer name or 'system'
    details     TEXT NOT NULL             -- JSON: before/after state
);

CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    state       TEXT NOT NULL,            -- JSON dialogue state
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dialogue_turns (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    role        TEXT NOT NULL CHECK (role IN ('user', 'agent')),
    text        TEXT NOT NULL,
    audio_path  TEXT,
    state_after TEXT,                     -- JSON snapshot after an agent turn
    created_at  TEXT NOT NULL
);
"""


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open a connection in autocommit mode; callers use explicit BEGIN IMMEDIATE."""
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_schema(db_path: Path | str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def seed_database(db_path: Path | str, seed_path: Path | str, reset: bool = False) -> None:
    """Create the schema and load providers, slots and sample bookings."""
    db_path = Path(db_path)
    if reset:
        for suffix in ("", "-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)
    init_schema(db_path)
    seed = json.loads(Path(seed_path).read_text())
    duration = timedelta(minutes=seed["slot_duration_minutes"])

    conn = connect(db_path)
    try:
        if conn.execute("SELECT COUNT(*) FROM providers").fetchone()[0]:
            return  # already seeded
        conn.execute("BEGIN IMMEDIATE")
        for p in seed["providers"]:
            conn.execute(
                "INSERT INTO providers (id, name, practitioner, service, keywords, address) VALUES (?, ?, ?, ?, ?, ?)",
                (p["id"], p["name"], p["practitioner"], p["service"], json.dumps(p["keywords"]), p["address"]),
            )
        for s in seed["slots"]:
            start = datetime.fromisoformat(s["start"]).astimezone(TZ)
            conn.execute(
                "INSERT INTO slots (id, provider_id, start_at, end_at, slot_date, slot_time) VALUES (?, ?, ?, ?, ?, ?)",
                (s["id"], s["provider_id"], start.isoformat(), (start + duration).isoformat(),
                 start.date().isoformat(), start.strftime("%H:%M")),
            )
        for b in seed["bookings"]:
            conn.execute("UPDATE slots SET status = 'booked' WHERE id = ?", (b["slot_id"],))
            conn.execute(
                "INSERT INTO bookings (id, slot_id, customer_name, status, session_id, created_at) "
                "VALUES (?, ?, ?, 'confirmed', NULL, ?)",
                (b["id"], b["slot_id"], b["customer_name"], b["created_at"]),
            )
            conn.execute(
                "INSERT INTO booking_audit (occurred_at, action, booking_id, slot_id, session_id, actor, details) "
                "VALUES (?, 'SEEDED', ?, ?, NULL, 'system', ?)",
                (b["created_at"], b["id"], b["slot_id"],
                 json.dumps({"before": {"slot_status": "available"},
                             "after": {"slot_status": "booked", "booking_status": "confirmed",
                                       "customer_name": b["customer_name"]}})),
            )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
