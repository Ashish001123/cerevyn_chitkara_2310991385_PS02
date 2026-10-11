import sqlite3
import threading
from contextlib import closing
from datetime import datetime

import pytest

from app.config import TZ, make_clock
from app.db import connect
from app.repository import Repository


def test_seed_has_three_providers_and_at_least_twelve_slots(repo):
    assert len(repo.services()) == 3
    with closing(connect(repo.db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM slots").fetchone()[0] >= 12
        starts = [r[0] for r in conn.execute("SELECT start_at FROM slots")]
    assert all(s.endswith("+05:30") for s in starts)


def test_available_slots_exclude_booked(repo):
    ids = {s["slot_id"] for s in repo.available_slots()}
    assert "S01" not in ids and "S07" not in ids   # seeded bookings
    assert len(ids) == 13


def test_reserve_then_second_reserve_fails(repo):
    first = repo.reserve_slot("S06", "Priya Sharma", "s1")
    second = repo.reserve_slot("S06", "Someone Else", "s2")
    assert first.ok and first.booking["booking_id"].startswith("BK")
    assert not second.ok and second.reason == "slot_taken"
    actions = [a["action"] for a in repo.audit_log() if a["slot_id"] == "S06"]
    assert actions == ["BOOKED", "BOOK_REJECTED_SLOT_TAKEN"]


def test_concurrent_reservations_only_one_wins(repo):
    barrier = threading.Barrier(25)
    results = []

    def attempt(i):
        barrier.wait()
        results.append(repo.reserve_slot("S09", f"Racer {i}", f"s{i}"))

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(25)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r.ok for r in results) == 1
    confirmed = [b for b in repo.list_bookings() if b["slot_id"] == "S09" and b["booking_status"] == "confirmed"]
    assert len(confirmed) == 1


def test_unique_index_blocks_second_confirmed_booking_even_bypassing_logic(repo):
    with closing(connect(repo.db_path)) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO bookings (id, slot_id, customer_name, status, created_at) "
                     "VALUES ('BK99999', 'S01', 'Intruder', 'confirmed', '2026-10-11T09:00:00+05:30')")


def test_cancel_frees_slot_and_is_audited(repo):
    result = repo.cancel_booking("BK24017", "s1")
    assert result.ok
    assert repo.get_slot("S07")["status"] == "available"
    assert repo.get_booking("BK24017")["booking_status"] == "cancelled"
    entry = repo.audit_log()[-1]
    assert entry["action"] == "CANCELLED"
    assert entry["details"]["before"]["booking_status"] == "confirmed"
    assert entry["details"]["after"]["slot_status"] == "available"
    again = repo.cancel_booking("BK24017", "s1")
    assert not again.ok and again.reason == "already_cancelled"
    assert repo.cancel_booking("BK00000", "s1").reason == "not_found"


def test_cancelled_slot_can_be_rebooked(repo):
    repo.cancel_booking("BK24017", None)
    assert repo.reserve_slot("S07", "New Customer", None).ok


def test_past_slots_are_not_offered_or_bookable(db_path):
    late = Repository(db_path, make_clock("2026-10-20T12:00:00+05:30"))
    ids = {s["slot_id"] for s in late.available_slots()}
    assert "S06" not in ids                                   # 20 Oct 10:00 already passed
    assert late.reserve_slot("S06", "Late", None).reason == "slot_in_past"


def test_alternatives_come_from_database_and_are_available(repo):
    alts = repo.alternatives("dental checkup", datetime(2026, 10, 20, 10, tzinfo=TZ))
    assert [a["slot_id"] for a in alts] == ["S02", "S03", "S04"]
    assert all(a["status"] == "available" for a in alts)
