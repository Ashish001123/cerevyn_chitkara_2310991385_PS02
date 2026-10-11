from datetime import date

import pytest

from app import nlu

TODAY = date(2026, 10, 11)
SERVICES = [
    {"service": "dental checkup", "keywords": ["dental", "dentist", "teeth"]},
    {"service": "haircut", "keywords": ["haircut", "hair cut", "salon"]},
    {"service": "physiotherapy", "keywords": ["physiotherapy", "physio"]},
]


def dt(text):
    _, digits = nlu.prepare(text)
    return nlu.parse_datetime(digits, TODAY)


@pytest.mark.parametrize("text, expected_date, expected_time", [
    ("October 20th at 10 AM", "2026-10-20", "10:00"),
    ("on the 20th of October at 10 a.m.", "2026-10-20", "10:00"),
    ("20 October 2026 at 2:30 p.m.", "2026-10-20", "14:30"),
    ("2026-10-21 at 15:00", "2026-10-21", "15:00"),
    ("21/10 at 3 pm", "2026-10-21", "15:00"),
    ("October twenty first at ten AM", "2026-10-21", "10:00"),
    ("tomorrow at noon", "2026-10-12", "12:00"),
    ("next Tuesday at 2 o'clock", "2026-10-13", "14:00"),
    ("October 20th 10 AM", "2026-10-20", "10:00"),
    ("half past 11 on October 20", "2026-10-20", "11:30"),
])
def test_parse_datetime(text, expected_date, expected_time):
    d, t = dt(text)
    assert d.isoformat() == expected_date
    assert t == expected_time


def test_date_without_time_and_past_date_rolls_to_next_year():
    d, t = dt("October 20th")
    assert (d.isoformat(), t) == ("2026-10-20", None)
    d, _ = dt("January 5th")
    assert d.isoformat() == "2027-01-05"


def test_bare_hour_only_when_expected():
    _, digits = nlu.prepare("2")
    assert nlu.parse_time(digits) is None
    assert nlu.parse_time(digits, allow_bare=True) == "14:00"
    assert nlu.parse_time(nlu.prepare("11")[1], allow_bare=True) == "11:00"


@pytest.mark.parametrize("text, service", [
    ("I need a dental checkup", "dental checkup"),
    ("book a haircut please", "haircut"),
    ("Physiotherapy fleet.", "physiotherapy"),
    ("physio therapy session", "physiotherapy"),
    ("hair cutt", "haircut"),
    ("I want to book an appointment", None),
])
def test_parse_service(text, service):
    assert nlu.parse_service(nlu.normalize(text), SERVICES) == service


@pytest.mark.parametrize("raw, expecting, name", [
    ("My name is Priya Sharma.", False, "Priya Sharma"),
    ("Hi, I'm Rohan Gupta and I need a dentist", False, "Rohan Gupta"),
    ("This is Meera Joshi.", False, "Meera Joshi"),
    ("i'm looking for a haircut", False, None),
    ("Karan Mehta.", True, "Karan Mehta"),
    ("Karan Mehta.", False, None),
    ("It's Anita", True, "Anita"),
    ("yes please", True, None),
])
def test_parse_name(raw, expecting, name):
    assert nlu.parse_name(raw, expecting, blocked_words={"haircut", "dental"}) == name


@pytest.mark.parametrize("text, context, answer", [
    ("Yes, please.", "book", True),
    ("Yeah go ahead", "book", True),
    ("Yes, confirmed.", "book", True),
    ("No, don't book it.", "book", False),
    ("Hmm, let me think.", "book", None),
    ("And let me think.", "book", None),
    ("cancel that", "book", False),
    ("Yes, cancel it.", "cancel", True),
    ("please cancel it", "cancel", True),
    ("no keep it", "cancel", False),
    ("", "book", None),
])
def test_parse_yes_no(text, context, answer):
    assert nlu.parse_yes_no(nlu.normalize(text), context) is answer


@pytest.mark.parametrize("text, expecting, booking_id", [
    ("My booking ID is BK24017.", False, "BK24017"),
    ("my booking id is B K 2 4 0 1 7", False, "BK24017"),
    ("booking BK-24017 please", False, "BK24017"),
    ("it's b.k. two four zero one seven", False, "BK24017"),
    ("booking number 24,017", False, "BK24017"),
    ("24017", True, "BK24017"),
    ("24017", False, None),
    ("BK2401", False, None),
])
def test_parse_booking_id(text, expecting, booking_id):
    assert nlu.parse_booking_id(nlu.prepare(text)[1], expecting) == booking_id


def test_intent_and_options():
    assert nlu.parse_intent("i want to cancel my booking") == "cancel"
    assert nlu.parse_intent("i would like to book an appointment") == "book"
    assert nlu.parse_intent("hello") is None
    assert nlu.parse_option_choice("the first one", 3) == 0
    assert nlu.parse_option_choice("option two please", 3) == 1
    assert nlu.parse_option_choice("the last one", 3) == 2
    assert nlu.parse_option_choice("the third one", 2) is None


@pytest.mark.parametrize("text, context, answer", [
    ("haan", "book", True),
    ("haan ji", "book", True),
    ("ji haan, book kar do", "book", True),
    ("theek hai", "book", True),
    ("sure thing", "book", True),
    ("yeah go for it", "book", True),
    ("that's right", "book", True),
    ("nahi", "book", False),
    ("nahin, mat karo", "book", False),
    ("na", "book", False),
    ("haan cancel kar do", "cancel", True),
])
def test_parse_yes_no_indian_english_and_hinglish(text, context, answer):
    assert nlu.parse_yes_no(nlu.normalize(text), context) is answer
