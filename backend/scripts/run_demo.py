"""End-to-end voice demo of every required scenario.

Each sample WAV in samples/audio is uploaded to the FastAPI voice endpoint, so
the run exercises real STT -> dialogue manager -> SQLite -> real TTS. Results are
written to demo_output/ (report.md, report.json, agent reply audio, booking
records and the audit log) and a set of consistency checks is printed.

Usage:  python scripts/run_demo.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
from contextlib import closing
from itertools import zip_longest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import ROOT, Settings, make_clock  # noqa: E402
from app.db import connect, seed_database  # noqa: E402
from app.main import create_app  # noqa: E402
from app.repository import Repository  # noqa: E402

FIXED_NOW = "2026-10-11T09:00:00+05:30"   # the seeded slots are 20-21 October 2026
OUT = ROOT / "demo_output"
DB = OUT / "demo.db"


def ordered_turns(scenario: dict) -> list[tuple[dict, dict]]:
    """Sequential turns, or round-robin across sessions when the scenario interleaves them."""
    sessions = scenario["sessions"]
    if not scenario.get("interleave"):
        return [(s, t) for s in sessions for t in s["turns"]]
    rounds = zip_longest(*[[(s, t) for t in s["turns"]] for s in sessions])
    return [pair for rnd in rounds for pair in rnd if pair]


def run_scenarios(client: TestClient, manifest: dict) -> list[dict]:
    audio_out = OUT / "audio"
    tts_dir = Path(client.app.state.tts_dir)
    results = []
    for scenario in manifest["scenarios"]:
        print(f"\n=== {scenario['title']} ===")
        session_ids, log = {}, []
        for session in scenario["sessions"]:
            r = client.post("/sessions").json()
            session_ids[session["label"]] = r["session_id"]
            log.append({"session": session["label"], "role": "agent", "text": r["reply_text"],
                        "audio": None, "step": r["state"]["step"]})
        for session, turn in ordered_turns(scenario):
            sid = session_ids[session["label"]]
            wav = ROOT / "samples" / "audio" / f"{turn['id']}.wav"
            with wav.open("rb") as f:
                resp = client.post(f"/sessions/{sid}/voice", files={"audio": (wav.name, f, "audio/wav")})
            resp.raise_for_status()
            r = resp.json()
            reply_audio = audio_out / f"{turn['id']}_reply.wav"
            shutil.copy(tts_dir / Path(r["reply_audio_url"]).name, reply_audio)
            print(f"[{session['label']}] USER  ({wav.name}) STT: {r['transcript']!r}")
            print(f"[{session['label']}] AGENT [{r['state']['step']}]: {r['reply_text']}")
            log.append({"session": session["label"], "role": "user", "spoken_text": turn["text"],
                        "audio": str(wav.relative_to(ROOT)), "transcript": r["transcript"]})
            log.append({"session": session["label"], "role": "agent", "text": r["reply_text"],
                        "audio": str(reply_audio.relative_to(ROOT)), "step": r["state"]["step"],
                        "state": r["state"]})
        results.append({"id": scenario["id"], "title": scenario["title"], "sessions": session_ids, "log": log})
    return results


def concurrency_stress(seed_path: Path) -> dict:
    """20 threads race for one slot through the repository; exactly one must win."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "race.db"
        seed_database(db, seed_path, reset=True)
        repo = Repository(db, make_clock(FIXED_NOW))
        barrier, outcomes = threading.Barrier(20), []

        def attempt(i: int) -> None:
            barrier.wait()
            outcomes.append(repo.reserve_slot("S09", f"Racer {i}", session_id=f"race-{i}"))

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        winners = [o.booking["booking_id"] for o in outcomes if o.ok]
        confirmed = [b for b in repo.list_bookings() if b["slot_id"] == "S09" and b["booking_status"] == "confirmed"]
        rejected = [a for a in repo.audit_log() if a["action"] == "BOOK_REJECTED_SLOT_TAKEN"]
        return {"slot": "S09 (haircut, 21 Oct 2026 10:00 IST)", "attempts": 20, "successes": len(winners),
                "winner": winners, "confirmed_bookings_for_slot": len(confirmed),
                "rejections_audited": len(rejected)}


def booked_slot_ids(repo: Repository) -> set[str]:
    with closing(connect(repo.db_path)) as conn:
        return {r[0] for r in conn.execute("SELECT id FROM slots WHERE status = 'booked'").fetchall()}


def confirmed_before_change(repo: Repository, audit_row: dict) -> bool:
    """The agent turn before the change must have been a confirmation prompt for the same target."""
    agent_turns = [t for t in repo.turns(audit_row["session_id"]) if t["role"] == "agent" and t["state_after"]]
    for prev, nxt in zip(agent_turns, agent_turns[1:]):
        bid = audit_row["booking_id"]
        result_phrase = f"booking ID is {bid}" if audit_row["action"] == "BOOKED" else f"Booking {bid} has been cancelled"
        if result_phrase not in nxt["text"]:
            continue
        s = prev["state_after"]
        if audit_row["action"] == "BOOKED":
            return s["step"] == "awaiting_booking_confirmation" and s["pending_slot_id"] == audit_row["slot_id"]
        return s["step"] == "awaiting_cancel_confirmation" and s["cancel_booking_id"] == audit_row["booking_id"]
    return False


def checks(repo: Repository, results: list[dict], race: dict) -> list[tuple[str, bool]]:
    by_slot: dict[str, list[dict]] = {}
    for b in repo.list_bookings():
        if b["booking_status"] == "confirmed":
            by_slot.setdefault(b["slot_id"], []).append(b)
    names = {slot: [b["customer_name"] for b in bs] for slot, bs in by_slot.items()}
    audit = repo.audit_log()
    double = next(r for r in results if r["id"] == "5_double_booking")
    declined = next(r for r in results if r["id"] == "6_declined_confirmation")
    vikram_final = [e for e in double["log"] if e["session"] == "Vikram" and e["role"] == "agent"][-1]
    sneha_final = [e for e in double["log"] if e["session"] == "Sneha" and e["role"] == "agent"][-1]
    return [
        ("1 Successful booking: haircut 20 Oct 10:00 (S06) booked for Priya",
         any(n.startswith("Priya") for n in names.get("S06", []))),
        ("2 Unavailable slot: dental 20 Oct 10:00 (S01) still holds only Anita's booking",
         names.get("S01") == ["Anita Desai"]),
        ("2 Unavailable slot: offered alternative 20 Oct 11:00 (S02) booked for Rohan",
         names.get("S02") == ["Rohan Gupta"]),
        ("3 Missing info: physiotherapy 20 Oct 14:00 (S13) booked for Karan",
         names.get("S13") == ["Karan Mehta"]),
        ("4 Cancellation: BK24017 cancelled and slot S07 available again",
         repo.get_booking("BK24017")["booking_status"] == "cancelled" and repo.get_slot("S07")["status"] == "available"),
        ("5 Same slot twice: Sneha got physiotherapy 21 Oct 10:00 (S14)",
         names.get("S14") == ["Sneha Kapoor"] and sneha_final["state"]["last_booking_id"] is not None),
        ("5 Same slot twice: Vikram's confirmation was rejected and audited",
         any(a["action"] == "BOOK_REJECTED_SLOT_TAKEN" and a["slot_id"] == "S14" and a["actor"] == "Vikram Singh"
             for a in audit)),
        ("5 Same slot twice: Vikram then booked a different offered slot",
         vikram_final["state"]["last_booking_id"] is not None and
         repo.get_booking(vikram_final["state"]["last_booking_id"])["slot_id"] != "S14"),
        ("6 Declined confirmation: agent asked to confirm S10, customer said no, slot still available",
         declined["log"][-3]["step"] == "awaiting_booking_confirmation"
         and declined["log"][-3]["state"]["pending_slot_id"] == "S10"
         and "not booked anything" in declined["log"][-1]["text"]
         and repo.get_slot("S10")["status"] == "available"),
        ("Every BOOKED/CANCELLED change followed a yes/no prompt for that exact slot or booking",
         all(confirmed_before_change(repo, a) for a in audit if a["action"] in ("BOOKED", "CANCELLED"))),
        ("No slot has more than one confirmed booking", all(len(v) == 1 for v in by_slot.values())),
        ("Booked slots match confirmed bookings exactly", booked_slot_ids(repo) == set(by_slot)),
        (f"Race: 20 concurrent reservations of one slot -> {race['successes']} success, "
         f"{race['rejections_audited']} audited rejections",
         race["successes"] == 1 and race["confirmed_bookings_for_slot"] == 1 and race["rejections_audited"] == 19),
    ]


def write_report(results, repo, race, check_results, engines) -> None:
    bookings, audit = repo.list_bookings(), repo.audit_log()
    (OUT / "booking_records.json").write_text(json.dumps(bookings, indent=2))
    (ROOT / "samples" / "booking_records.json").write_text(json.dumps(bookings, indent=2))
    (OUT / "audit_log.json").write_text(json.dumps(audit, indent=2))
    (OUT / "report.json").write_text(json.dumps({"engines": engines, "scenarios": results, "race": race,
                                                 "checks": check_results}, indent=2))
    lines = ["# PS02 Voice Agent: Demo Report", "",
             f"Clock fixed at `{FIXED_NOW}` (Asia/Kolkata). STT: `{engines['stt']}`, TTS: `{engines['tts']}`.", "",
             "Every user turn is a WAV file uploaded to `POST /sessions/{id}/voice`, transcribed by the STT "
             "engine, handled by the dialogue manager against SQLite, and answered with synthesized audio.", ""]
    for r in results:
        lines += [f"## {r['title']}", "", "| Who | Audio | Text | Dialogue step |", "|---|---|---|---|"]
        for e in r["log"]:
            if e["role"] == "user":
                lines.append(f"| {e['session']} | `{e['audio']}` | STT: “{e['transcript']}” | |")
            else:
                audio = f"`{e['audio']}`" if e["audio"] else ""
                lines.append(f"| Agent → {e['session']} | {audio} | {e['text']} | `{e['step']}` |")
        lines.append("")
    lines += ["## Concurrency stress test", "", "```json", json.dumps(race, indent=2), "```", "",
              "## Checks", ""] + [f"- {'PASS' if ok else 'FAIL'}: {name}" for name, ok in check_results]
    lines += ["", "## Booking records after the demo", "",
              "| Booking ID | Customer | Service | Start (IST) | Status |", "|---|---|---|---|---|"]
    lines += [f"| {b['booking_id']} | {b['customer_name']} | {b['service']} | {b['start_at']} | {b['booking_status']} |"
              for b in bookings]
    lines += ["", "## Audit log", "", "| # | Time | Action | Booking | Slot | Actor |", "|---|---|---|---|---|---|"]
    lines += [f"| {a['id']} | {a['occurred_at']} | {a['action']} | {a['booking_id'] or ''} | {a['slot_id']} | "
              f"{a['actor']} |" for a in audit]
    (OUT / "report.md").write_text("\n".join(lines) + "\n")


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "audio").mkdir(parents=True)
    settings = Settings(db_path=DB, audio_out_dir=OUT / "tts_tmp", app_now=FIXED_NOW)
    seed_database(DB, settings.seed_path, reset=True)
    app = create_app(settings)
    engines = {"stt": app.state.stt.name, "tts": app.state.tts.name}
    print(f"STT engine: {engines['stt']} | TTS engine: {engines['tts']}")
    manifest = json.loads((ROOT / "samples" / "requests.json").read_text())
    with TestClient(app) as client:
        results = run_scenarios(client, manifest)
    repo = Repository(DB, make_clock(FIXED_NOW))
    race = concurrency_stress(settings.seed_path)
    check_results = checks(repo, results, race)
    write_report(results, repo, race, check_results, engines)
    shutil.rmtree(OUT / "tts_tmp", ignore_errors=True)
    shutil.rmtree(OUT / "uploads", ignore_errors=True)
    print("\n=== Checks ===")
    for name, ok in check_results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    print(f"\nReport: {(OUT / 'report.md').relative_to(ROOT)}")
    return 0 if all(ok for _, ok in check_results) else 1


if __name__ == "__main__":
    sys.exit(main())
