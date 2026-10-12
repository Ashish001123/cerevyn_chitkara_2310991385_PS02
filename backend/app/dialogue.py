"""Slot-filling dialogue manager with explicit confirmation before any booking change.

State lives in the `sessions` table and is reloaded on every turn, so a session
survives process restarts and can be served by any API replica.
"""

from __future__ import annotations

import copy
import threading
from datetime import date, datetime
from typing import Callable

from . import nlu
from .config import TZ
from .repository import Repository

REQUIRED_FIELDS = ("service", "customer_name", "date", "time")

GREETING = "Hi! I can book or cancel a dental checkup, haircut or physiotherapy appointment. How can I help?"


def initial_state() -> dict:
    return {
        "step": "start",            # start | collecting | offering_alternatives |
                                    # awaiting_booking_confirmation | awaiting_cancel_confirmation
        "intent": None,             # book | cancel
        "awaiting": "intent",       # which piece of information the agent last asked for
        "service": None,
        "customer_name": None,
        "date": None,               # YYYY-MM-DD, Asia/Kolkata
        "time": None,               # HH:MM, Asia/Kolkata
        "pending_slot_id": None,    # slot read back to the user and awaiting yes/no
        "alternatives": [],         # slot ids offered from the database
        "cancel_booking_id": None,
        "last_booking_id": None,
        "last_cancelled_id": None,
        "turn": 0,
    }


def spoken_time(hhmm: str) -> str:
    """'10:00' -> '10 AM', '14:30' -> '2:30 PM'."""
    t = datetime.strptime(hhmm, "%H:%M")
    clock = t.strftime("%I").lstrip("0") + (t.strftime(":%M") if t.minute else "")
    return f"{clock} {t.strftime('%p')}"


def spoken_date(iso: str, this_year: int | None = None) -> str:
    """'2026-10-20' -> 'Tuesday 20 October'; the year is said only when it is not this year."""
    d = date.fromisoformat(iso)
    this_year = this_year or datetime.now(TZ).year
    return f"{d.strftime('%A')} {d.day} {d.strftime('%B')}" + ("" if d.year == this_year else f" {d.year}")


def spoken_when(start_at: str, this_year: int | None = None) -> str:
    dt = datetime.fromisoformat(start_at).astimezone(TZ)
    return f"{spoken_date(dt.date().isoformat(), this_year)} at {spoken_time(dt.strftime('%H:%M'))}"


def spoken_list(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " or " + items[-1]


class DialogueManager:
    def __init__(self, repo: Repository, clock: Callable[[], datetime]):
        self.repo = repo
        self.clock = clock
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def start_session(self, speak: Callable[[str], str | None] | None = None) -> tuple[str, str, dict, str | None]:
        state = initial_state()
        session_id = self.repo.create_session(state)
        audio = speak(GREETING) if speak else None
        self.repo.add_turn(session_id, "agent", GREETING, audio_path=audio, state_after=state)
        return session_id, GREETING, state, audio

    def _lock(self, session_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(session_id, threading.Lock())

    def handle(self, session_id: str, user_text: str, user_audio: str | None = None,
               speak: Callable[[str], str | None] | None = None) -> tuple[str, dict, str | None]:
        """Process one user utterance; turns of the same session are serialized."""
        with self._lock(session_id):
            state = self.repo.load_state(session_id)
            if state is None:
                raise KeyError(session_id)
            self.repo.add_turn(session_id, "user", user_text, audio_path=user_audio)
            new_state = copy.deepcopy(state)
            new_state["turn"] += 1
            reply = self._process(session_id, new_state, user_text)
            self.repo.save_state(session_id, new_state)
            reply_audio = speak(reply) if speak else None
            self.repo.add_turn(session_id, "agent", reply, audio_path=reply_audio, state_after=new_state)
            return reply, new_state, reply_audio

    def _process(self, session_id: str, state: dict, raw: str) -> str:
        norm, digits = nlu.prepare(raw)
        if not norm:
            return "Sorry, I didn't catch that. Could you say it again?"
        if nlu.is_reset(norm):
            state.clear()
            state.update(initial_state())
            return "Okay, let's start over. Would you like to book or cancel an appointment?"

        return self._on_collect(state, raw, norm, digits)

    def _today(self) -> date:
        return self.clock().astimezone(TZ).date()

    def _blocked_name_words(self) -> set[str]:
        words = set(nlu.MONTHS) | set(nlu.WEEKDAYS)
        for s in self.repo.services():
            for kw in s["keywords"] + [s["service"]]:
                words.update(kw.lower().split())
        return words

    def _extract_booking_fields(self, state: dict, raw: str, norm: str, digits: str) -> list[str]:
        """Merge any service/name/date/time in the utterance into state. Returns changed fields."""
        changed = []
        service = nlu.parse_service(norm, self.repo.services())
        name = nlu.parse_name(raw, expecting=state["awaiting"] == "customer_name",
                              blocked_words=self._blocked_name_words())
        d, t = nlu.parse_datetime(digits, self._today(), expecting=state["awaiting"])
        for key, value in (("service", service), ("customer_name", name),
                           ("date", d.isoformat() if d else None), ("time", t)):
            if value and value != state[key]:
                state[key] = value
                changed.append(key)
        return changed

    def _on_collect(self, state: dict, raw: str, norm: str, digits: str) -> str:
        if state["intent"] is None or (state["intent"] == "book" and nlu.parse_intent(norm) == "cancel"):
            intent = nlu.parse_intent(norm)
            state["intent"] = intent

        changed = self._extract_booking_fields(state, raw, norm, digits)
        if state["intent"] is None:
            if not changed:
                state["awaiting"] = "intent"
                return "I can book or cancel an appointment. Which would you like?"
            state["intent"] = "book"
        state["step"] = "collecting"
        return self._advance_booking(state)

    def _advance_booking(self, state: dict, prefix: str = "") -> str:
        """Ask for the next missing field."""
        if state["date"] and date.fromisoformat(state["date"]) < self._today():
            state["date"] = None
            prefix += "That date has already passed. "

        missing = [f for f in REQUIRED_FIELDS if not state[f]]
        if not missing:
            return prefix + "All details collected."

        field = missing[0]
        state["awaiting"] = field
        first_name = (state["customer_name"] or "").split(" ")[0]
        ack = f"Thanks, {first_name}. " if field == "date" and first_name else ""
        if field == "service":
            options = spoken_list([s["service"] for s in self.repo.services()])
            return prefix + f"Sure. Which service: {options}?"
        if field == "customer_name":
            return prefix + f"Sure, a {state['service']} appointment. What's your name?"
        if field == "date":
            dates = sorted({s["slot_date"] for s in self.repo.available_slots(state["service"])})
            if not dates:
                return prefix + f"Sorry, there are no open {state['service']} slots right now."
            listed = spoken_list([spoken_date(d, self.clock().year) for d in dates[:4]])
            return prefix + ack + f"Which day? I have openings on {listed}."
        # field == "time"
        slots = self.repo.available_slots(state["service"], state["date"])
        if not slots:
            return prefix + f"There are no open {state['service']} slots on {spoken_date(state['date'], self.clock().year)}."
        times = spoken_list([spoken_time(s["slot_time"]) for s in slots])
        return prefix + ack + f"What time on {spoken_date(state['date'], self.clock().year)}? I have {times}."
