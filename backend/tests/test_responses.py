"""Reply style: short, natural phrasing that still reads back the exact slot before any change."""

import re

import pytest

from app.dialogue import GREETING, spoken_date, spoken_time, spoken_when


def words(text):
    return len(text.split())


@pytest.mark.parametrize("hhmm, spoken", [
    ("10:00", "10 AM"), ("14:00", "2 PM"), ("14:30", "2:30 PM"), ("12:00", "12 PM"), ("09:15", "9:15 AM"),
])
def test_times_are_spoken_naturally(hhmm, spoken):
    assert spoken_time(hhmm) == spoken


def test_dates_omit_the_current_year():
    assert spoken_date("2026-10-20") == "Tuesday 20 October"
    assert spoken_date("2027-01-05") == "Tuesday 5 January 2027"
    assert spoken_when("2026-10-20T10:00:00+05:30") == "Tuesday 20 October at 10 AM"


def test_greeting_is_short():
    assert words(GREETING) <= 22


def test_alternatives_are_compact_and_say_ist_once(convo):
    c = convo()
    reply = c.say("Dental checkup on October 20th at 10 AM, my name is Rohan Gupta.")
    assert c.state["step"] == "offering_alternatives"
    assert words(reply) <= 40
    assert "2026" not in reply
    assert reply.count("IST") <= 1
    for t in ("11 AM", "2 PM", "10 AM"):
        assert t in reply


def test_confirmation_reads_back_the_exact_slot(convo):
    c = convo()
    reply = c.say("Haircut on October 20th at 10 AM, my name is Priya Sharma.")
    assert "Tuesday 20 October at 10 AM" in reply
    assert "Priya" in reply
    assert reply.rstrip().endswith("?")
    assert words(reply) <= 30


def test_booking_confirmation_keeps_id_and_details(convo):
    c = convo()
    c.say("Haircut on October 20th at 10 AM, my name is Priya Sharma.")
    reply = c.say("yes")
    assert re.search(r"booking ID is BK\d{5}", reply)
    assert "Tuesday 20 October at 10 AM IST" in reply
    assert words(reply) <= 40


def test_every_prompt_in_a_slot_filling_call_is_short(convo):
    c = convo()
    replies = [c.say(t) for t in ("I would like to book an appointment.", "Physiotherapy, please.",
                                  "Karan Mehta.", "October 20th.", "2 PM.", "Hmm.", "Yes.")]
    for reply in replies[:-1]:
        assert words(reply) <= 30, reply
        assert len(re.findall(r"[.?!](\s|$)", reply)) <= 3, reply
