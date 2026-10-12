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
        """Process one user utterance; turns of the same session are serialized.

        `speak` turns the reply into audio and returns its path, which is stored
        with the agent turn. Returns (reply_text, new_state, reply_audio_path).
        """
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

        step = state["step"]
        if step == "awaiting_booking_confirmation":
            return self._on_booking_confirmation(session_id, state, raw, norm, digits)
        if step == "awaiting_cancel_confirmation":
            return self._on_cancel_confirmation(session_id, state, norm)
        if step == "offering_alternatives":
            return self._on_alternatives(state, raw, norm, digits)
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
            if intent == "cancel":
                state.update(initial_state() | {"customer_name": state["customer_name"], "turn": state["turn"],
                                                 "last_booking_id": state["last_booking_id"]})
            state["intent"] = intent
        if state["intent"] == "cancel":
            return self._collect_cancel(state, digits)

        changed = self._extract_booking_fields(state, raw, norm, digits)
        if state["intent"] is None:
            if not changed:
                state["awaiting"] = "intent"
                return "I can book or cancel an appointment. Which would you like?"
            state["intent"] = "book"
        state["step"] = "collecting"
        return self._advance_booking(state)

    def _advance_booking(self, state: dict, prefix: str = "") -> str:
        """Ask for the next missing field, or check the database once everything is known."""
        if state["date"] and date.fromisoformat(state["date"]) < self._today():
            state["date"] = None
            prefix += "That date has already passed. "

        missing = [f for f in REQUIRED_FIELDS if not state[f]]
        if not missing:
            return prefix + self._check_availability(state)

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
                self._reset_booking_fields(state)
                return prefix + f"Sorry, there are no open {state['service']} slots right now."
            listed = spoken_list([self._date(d) for d in dates[:4]])
            return prefix + ack + f"Which day? I have openings on {listed}."
        # field == "time"
        slots = self.repo.available_slots(state["service"], state["date"])
        if not slots:
            return prefix + self._offer_alternatives(
                state, f"There are no open {state['service']} slots on {self._date(state['date'])}.")
        times = spoken_list([spoken_time(s["slot_time"]) for s in slots])
        return prefix + ack + f"What time on {self._date(state['date'])}? I have {times}."

    def _requested_dt(self, state: dict) -> datetime | None:
        if not state["date"]:
            return None
        hhmm = state["time"] or "12:00"
        return datetime.fromisoformat(f"{state['date']}T{hhmm}:00").replace(tzinfo=TZ)

    def _check_availability(self, state: dict) -> str:
        slot = self.repo.find_slot(state["service"], state["date"], state["time"])
        requested = self._when(self._requested_dt(state).isoformat())
        if slot and slot["status"] == "available" and slot["start_at"] > self.clock().isoformat():
            return self._ask_booking_confirmation(state, slot)
        if slot is None:
            reason = f"There is no {state['service']} slot at {requested}."
        elif slot["status"] == "booked":
            reason = f"Sorry, {requested} is already booked."
        else:
            reason = f"Sorry, {requested} has already passed."
        return self._offer_alternatives(state, reason)

    def _offer_alternatives(self, state: dict, reason: str, exclude: set[str] | None = None) -> str:
        alts = self.repo.alternatives(state["service"], self._requested_dt(state), limit=3, exclude=exclude)
        state["pending_slot_id"] = None
        if not alts:
            state.update(step="collecting", awaiting="date", alternatives=[], date=None, time=None)
            return f"{reason} Sorry, there are no other open {state['service']} slots."
        state.update(step="offering_alternatives", awaiting="alternative_choice",
                     alternatives=[a["slot_id"] for a in alts])
        return f"{reason} I have {self._options(alts)}. Which works for you?"

    def _options(self, slots: list[dict]) -> str:
        """'11 AM or 2 PM on Tuesday 20 October, or 10 AM on Wednesday 21 October' (slot order kept)."""
        groups: dict[str, list[str]] = {}
        for s in slots:
            groups.setdefault(s["slot_date"], []).append(spoken_time(s["slot_time"]))
        return ", or ".join(f"{spoken_list(times)} on {self._date(d)}" for d, times in groups.items())

    def _date(self, iso: str) -> str:
        return spoken_date(iso, self.clock().year)

    def _when(self, start_at: str) -> str:
        return spoken_when(start_at, self.clock().year)

    def _ask_booking_confirmation(self, state: dict, slot: dict) -> str:
        state.update(step="awaiting_booking_confirmation", awaiting="confirmation",
                     pending_slot_id=slot["slot_id"], alternatives=[],
                     date=slot["slot_date"], time=slot["slot_time"])
        first_name = state["customer_name"].split(" ")[0]
        return (f"{slot['service'].capitalize()} with {slot['practitioner']} at {slot['provider_name']}, "
                f"{self._when(slot['start_at'])}. Shall I book it for {first_name}?")


    def _on_alternatives(self, state: dict, raw: str, norm: str, digits: str) -> str:
        offered = [s for s in (self.repo.get_slot(i) for i in state["alternatives"]) if s]
        choice = nlu.parse_option_choice(norm, len(offered))
        if choice is not None:
            return self._choose(state, offered[choice])

        service = nlu.parse_service(norm, self.repo.services())
        if service and service != state["service"]:
            state.update(service=service, step="collecting", alternatives=[])
            self._extract_booking_fields(state, raw, norm, digits)
            return self._advance_booking(state)

        d, t = nlu.parse_datetime(digits, self._today(), allow_bare_time=True)
        if t:
            matches = [s for s in offered if s["slot_time"] == t and (d is None or s["slot_date"] == d.isoformat())]
            if len(matches) == 1:
                return self._choose(state, matches[0])
            state.update(date=d.isoformat() if d else state["date"], time=t, step="collecting", alternatives=[])
            return self._advance_booking(state)
        if d:
            state.update(date=d.isoformat(), time=None, step="collecting", alternatives=[])
            return self._advance_booking(state)

        answer = nlu.parse_yes_no(norm, "book")
        if answer is True and len(offered) == 1:
            return self._choose(state, offered[0])
        if answer is False:
            state.update(step="collecting", awaiting="date", alternatives=[], date=None, time=None)
            return "No problem. Which other date would you like?"
        return f"Sorry, which one? I have {self._options(offered)}."

    def _choose(self, state: dict, slot: dict) -> str:
        fresh = self.repo.get_slot(slot["slot_id"])  # re-read: it may have been taken meanwhile
        if fresh is None or fresh["status"] != "available":
            return self._offer_alternatives(state, "Sorry, that slot has just been taken.", exclude={slot["slot_id"]})
        return self._ask_booking_confirmation(state, fresh)


    def _on_booking_confirmation(self, session_id: str, state: dict, raw: str, norm: str, digits: str) -> str:
        pending = self.repo.get_slot(state["pending_slot_id"])
        before = {k: state[k] for k in ("service", "date", "time")}
        changed = [k for k in self._extract_booking_fields(state, raw, norm, digits) if k in before]
        if changed:
            # The user asked for something different instead of answering yes/no.
            if "date" in changed and "time" not in changed:
                state["time"] = None
            state.update(step="collecting", pending_slot_id=None)
            return self._advance_booking(state, prefix="Okay, I haven't booked anything yet. ")

        answer = nlu.parse_yes_no(norm, "book")
        if answer is None:
            return f"Sorry, is that a yes or a no? Shall I book {self._when(pending['start_at'])}?"
        if answer is False:
            state.update(step="collecting", awaiting="date", pending_slot_id=None, date=None, time=None)
            return "No problem, I have not booked anything. Which other day would you like?"

        result = self.repo.reserve_slot(state["pending_slot_id"], state["customer_name"], session_id)
        if not result.ok:
            reason = ("Sorry, that slot was just booked by another customer, so I could not reserve it."
                      if result.reason == "slot_taken" else "Sorry, that slot is no longer available.")
            return self._offer_alternatives(state, reason, exclude={state["pending_slot_id"]})

        b = result.booking
        state["last_booking_id"] = b["booking_id"]
        self._reset_booking_fields(state)
        return (f"You're confirmed! Your booking ID is {b['booking_id']}. {b['service'].capitalize()} with "
                f"{b['practitioner']} at {b['provider_name']}, {self._when(b['start_at'])} IST. Anything else?")

    def _reset_booking_fields(self, state: dict) -> None:
        state.update(step="start", intent=None, awaiting="intent", service=None, date=None, time=None,
                     pending_slot_id=None, alternatives=[], cancel_booking_id=None)


    def _collect_cancel(self, state: dict, digits: str) -> str:
        state["step"] = "collecting"
        booking_id = nlu.parse_booking_id(digits, expecting=state["awaiting"] == "booking_id")
        if booking_id is None:
            state["awaiting"] = "booking_id"
            return "Sure. What's your booking ID? It starts with B K."
        booking = self.repo.get_booking(booking_id)
        if booking is None:
            state["awaiting"] = "booking_id"
            return f"I couldn't find booking {booking_id}. Could you say the ID again?"
        if booking["booking_status"] != "confirmed":
            self._reset_booking_fields(state)
            return f"Booking {booking_id} is already cancelled. Anything else?"
        state.update(step="awaiting_cancel_confirmation", awaiting="confirmation", cancel_booking_id=booking_id)
        return (f"I found {booking_id}: {booking['service']} for {booking['customer_name']}, "
                f"{self._when(booking['start_at'])}. Shall I cancel it?")

    def _on_cancel_confirmation(self, session_id: str, state: dict, norm: str) -> str:
        booking_id = state["cancel_booking_id"]
        answer = nlu.parse_yes_no(norm, "cancel")
        if answer is None:
            return f"Sorry, is that a yes or a no? Should I cancel {booking_id}?"
        if answer is False:
            self._reset_booking_fields(state)
            return f"Okay, I have not cancelled anything. Booking {booking_id} is still confirmed."
        result = self.repo.cancel_booking(booking_id, session_id)
        self._reset_booking_fields(state)
        if not result.ok:
            return f"Booking {booking_id} could not be cancelled because it is {result.reason.replace('_', ' ')}."
        state["last_cancelled_id"] = booking_id
        b = result.booking
        return (f"Done. Booking {booking_id} has been cancelled, and {self._when(b['start_at'])} "
                f"is free again. Anything else?")
