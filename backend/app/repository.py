"""Data access: availability, atomic reservation, cancellation, audit and dialogue state."""

from __future__ import annotations

import json
import secrets
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .config import TZ
from .db import connect

SLOT_SELECT = """
SELECT s.id AS slot_id, s.start_at, s.end_at, s.slot_date, s.slot_time, s.status,
       p.id AS provider_id, p.name AS provider_name, p.practitioner, p.service, p.address
FROM slots s JOIN providers p ON p.id = s.provider_id
"""


@dataclass
class ReserveResult:
    ok: bool
    booking: dict | None = None
    reason: str | None = None  # "slot_taken" | "slot_not_found" | "slot_in_past"


@dataclass
class CancelResult:
    ok: bool
    booking: dict | None = None
    reason: str | None = None  # "not_found" | "already_cancelled"


class Repository:
    def __init__(self, db_path: Path | str, clock: Callable[[], datetime]):
        self.db_path = db_path
        self.clock = clock

    def _conn(self) -> sqlite3.Connection:
        return connect(self.db_path)

    def _now_iso(self) -> str:
        return self.clock().astimezone(TZ).isoformat(timespec="seconds")

    def services(self) -> list[dict]:
        with closing(self._conn()) as conn:
            rows = conn.execute("SELECT * FROM providers ORDER BY id").fetchall()
        return [{**dict(r), "keywords": json.loads(r["keywords"])} for r in rows]

    def get_slot(self, slot_id: str) -> dict | None:
        with closing(self._conn()) as conn:
            row = conn.execute(SLOT_SELECT + " WHERE s.id = ?", (slot_id,)).fetchone()
        return dict(row) if row else None

    def find_slot(self, service: str, slot_date: str, slot_time: str) -> dict | None:
        """Exact slot for service/date/time regardless of status, or None if no such slot exists."""
        with closing(self._conn()) as conn:
            row = conn.execute(
                SLOT_SELECT + " WHERE p.service = ? AND s.slot_date = ? AND s.slot_time = ?",
                (service, slot_date, slot_time),
            ).fetchone()
        return dict(row) if row else None

    def available_slots(self, service: str | None = None, slot_date: str | None = None) -> list[dict]:
        """Future, unbooked slots read straight from the database."""
        sql = SLOT_SELECT + " WHERE s.status = 'available' AND s.start_at > ?"
        params: list[Any] = [self._now_iso()]
        if service:
            sql += " AND p.service = ?"
            params.append(service)
        if slot_date:
            sql += " AND s.slot_date = ?"
            params.append(slot_date)
        sql += " ORDER BY s.start_at, p.id"
        with closing(self._conn()) as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def alternatives(self, service: str, requested: datetime | None, limit: int = 3,
                     exclude: set[str] | None = None) -> list[dict]:
        """Closest available slots to the requested time, never fabricated."""
        exclude = exclude or set()
        slots = [s for s in self.available_slots(service) if s["slot_id"] not in exclude]
        if requested is not None:
            slots.sort(key=lambda s: abs((datetime.fromisoformat(s["start_at"]) - requested).total_seconds()))
        return sorted(slots[:limit], key=lambda s: s["start_at"])

    def reserve_slot(self, slot_id: str, customer_name: str, session_id: str | None) -> ReserveResult:
        """Atomically claim a slot. Exactly one concurrent caller can succeed.

        BEGIN IMMEDIATE takes SQLite's write lock up front, the conditional UPDATE
        only flips a slot that is still 'available', and the partial unique index on
        bookings(slot_id) rejects a second confirmed booking even if that logic regressed.
        """
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            slot = conn.execute(SLOT_SELECT + " WHERE s.id = ?", (slot_id,)).fetchone()
            if slot is None:
                conn.execute("ROLLBACK")
                return ReserveResult(False, reason="slot_not_found")
            if slot["start_at"] <= self._now_iso():
                conn.execute("ROLLBACK")
                return ReserveResult(False, reason="slot_in_past")
            claimed = conn.execute(
                "UPDATE slots SET status = 'booked' WHERE id = ? AND status = 'available'", (slot_id,)
            ).rowcount
            if claimed != 1:
                self._audit(conn, "BOOK_REJECTED_SLOT_TAKEN", None, slot_id, session_id, customer_name,
                            {"reason": "slot already booked", "requested_by": customer_name})
                conn.execute("COMMIT")
                return ReserveResult(False, reason="slot_taken")
            now = self._now_iso()
            booking_id = self._insert_booking(conn, slot_id, customer_name, session_id, now)
            self._audit(conn, "BOOKED", booking_id, slot_id, session_id, customer_name,
                        {"before": {"slot_status": "available"},
                         "after": {"slot_status": "booked", "booking_status": "confirmed",
                                   "customer_name": customer_name, "start_at": slot["start_at"],
                                   "service": slot["service"]}})
            conn.execute("COMMIT")
        except sqlite3.IntegrityError:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            return ReserveResult(False, reason="slot_taken")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return ReserveResult(True, booking=self.get_booking(booking_id))

    @staticmethod
    def _insert_booking(conn, slot_id, customer_name, session_id, now) -> str:
        for _ in range(20):
            booking_id = f"BK{secrets.randbelow(90000) + 10000}"
            if conn.execute("SELECT 1 FROM bookings WHERE id = ?", (booking_id,)).fetchone():
                continue
            conn.execute(
                "INSERT INTO bookings (id, slot_id, customer_name, status, session_id, created_at) "
                "VALUES (?, ?, ?, 'confirmed', ?, ?)",
                (booking_id, slot_id, customer_name, session_id, now),
            )
            return booking_id
        raise RuntimeError("could not allocate a unique booking id")

    def get_booking(self, booking_id: str) -> dict | None:
        with closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT b.id AS booking_id, b.customer_name, b.status AS booking_status, b.created_at,"
                " b.cancelled_at, b.session_id, s.id AS slot_id, s.start_at, s.slot_date, s.slot_time,"
                " s.status AS slot_status, p.name AS provider_name, p.practitioner, p.service, p.address"
                " FROM bookings b JOIN slots s ON s.id = b.slot_id JOIN providers p ON p.id = s.provider_id"
                " WHERE b.id = ?",
                (booking_id.upper(),),
            ).fetchone()
        return dict(row) if row else None

    def list_bookings(self) -> list[dict]:
        with closing(self._conn()) as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM bookings ORDER BY created_at").fetchall()]
        return [self.get_booking(i) for i in ids]

    def cancel_booking(self, booking_id: str, session_id: str | None) -> CancelResult:
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id.upper(),)).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return CancelResult(False, reason="not_found")
            if row["status"] != "confirmed":
                conn.execute("ROLLBACK")
                return CancelResult(False, booking=self.get_booking(row["id"]), reason="already_cancelled")
            now = self._now_iso()
            conn.execute("UPDATE bookings SET status = 'cancelled', cancelled_at = ? WHERE id = ?", (now, row["id"]))
            conn.execute("UPDATE slots SET status = 'available' WHERE id = ?", (row["slot_id"],))
            self._audit(conn, "CANCELLED", row["id"], row["slot_id"], session_id, row["customer_name"],
                        {"before": {"booking_status": "confirmed", "slot_status": "booked"},
                         "after": {"booking_status": "cancelled", "slot_status": "available"}})
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return CancelResult(True, booking=self.get_booking(row["id"]))


    def _audit(self, conn, action, booking_id, slot_id, session_id, actor, details) -> None:
        conn.execute(
            "INSERT INTO booking_audit (occurred_at, action, booking_id, slot_id, session_id, actor, details) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self._now_iso(), action, booking_id, slot_id, session_id, actor, json.dumps(details)),
        )

    def audit_log(self) -> list[dict]:
        with closing(self._conn()) as conn:
            rows = conn.execute("SELECT * FROM booking_audit ORDER BY id").fetchall()
        return [{**dict(r), "details": json.loads(r["details"])} for r in rows]


    def create_session(self, state: dict) -> str:
        session_id = uuid.uuid4().hex
        now = self._now_iso()
        with closing(self._conn()) as conn:
            conn.execute("INSERT INTO sessions (id, state, created_at, updated_at) VALUES (?, ?, ?, ?)",
                         (session_id, json.dumps(state), now, now))
        return session_id

    def load_state(self, session_id: str) -> dict | None:
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT state FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return json.loads(row["state"]) if row else None

    def save_state(self, session_id: str, state: dict) -> None:
        with closing(self._conn()) as conn:
            conn.execute("UPDATE sessions SET state = ?, updated_at = ? WHERE id = ?",
                         (json.dumps(state), self._now_iso(), session_id))

    def add_turn(self, session_id: str, role: str, text: str, audio_path: str | None = None,
                 state_after: dict | None = None) -> None:
        with closing(self._conn()) as conn:
            conn.execute(
                "INSERT INTO dialogue_turns (session_id, role, text, audio_path, state_after, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, role, text, audio_path, json.dumps(state_after) if state_after else None,
                 self._now_iso()),
            )

    def turns(self, session_id: str) -> list[dict]:
        with closing(self._conn()) as conn:
            rows = conn.execute("SELECT * FROM dialogue_turns WHERE session_id = ? ORDER BY id",
                                (session_id,)).fetchall()
        return [{**dict(r), "state_after": json.loads(r["state_after"]) if r["state_after"] else None}
                for r in rows]
