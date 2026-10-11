"""Spoken date/time understanding: the corpus behind Phase 1 of the call-quality plan.

`expecting` mirrors the dialogue's `awaiting` field: what the agent just asked for.
"""

from datetime import date

import pytest

from app import nlu

TODAY = date(2026, 10, 11)  # Sunday


def parse(text, expecting=None):
    _, digits = nlu.prepare(text)
    d, t = nlu.parse_datetime(digits, TODAY, expecting=expecting)
    return (d.isoformat() if d else None), t


# ------------------------------------------------------------------ full phrases
@pytest.mark.parametrize("text, expected", [
    # explicit month (regression: these already worked)
    ("October 20th at 10 AM", ("2026-10-20", "10:00")),
    ("on the 20th of October at 10 a.m.", ("2026-10-20", "10:00")),
    ("2:00 p.m. on October the 20th", ("2026-10-20", "14:00")),
    ("20/10 10:30", ("2026-10-20", "10:30")),
    ("2026-10-21 at 15:00", ("2026-10-21", "15:00")),
    ("day after tomorrow at 11", ("2026-10-13", "11:00")),
    # day without a month -> nearest upcoming occurrence
    ("the 20th at 10", ("2026-10-20", "10:00")),
    ("20th at 2 pm", ("2026-10-20", "14:00")),
    ("on 21st", ("2026-10-21", None)),
    ("the 20th of this month at 11", ("2026-10-20", "11:00")),
    ("at 10 on the 20th", ("2026-10-20", "10:00")),
    ("20 at 2", ("2026-10-20", "14:00")),
    ("the 5th at 3", ("2026-11-05", "15:00")),          # 5 Oct already passed
    # times without a colon
    ("October 20th at 10.30 am", ("2026-10-20", "10:30")),
    ("October 20 at 2 30 pm", ("2026-10-20", "14:30")),
    ("October 20 at 2 30", ("2026-10-20", "14:30")),
    ("October 20 10 30", ("2026-10-20", "10:30")),
    ("October 20 at 1030", ("2026-10-20", "10:30")),
    ("October 20, 2 o clock", ("2026-10-20", "14:00")),
    ("October 20th, 10:00 o'clock", ("2026-10-20", "10:00")),
    # spoken numbers and phrases
    ("October twentieth at two thirty", ("2026-10-20", "14:30")),
    ("ten in the morning on the twentieth", ("2026-10-20", "10:00")),
    ("the twenty first at quarter to three", ("2026-10-21", "14:45")),
    ("October 20 at quarter past 10", ("2026-10-20", "10:15")),
    ("October 21st at half past 2", ("2026-10-21", "14:30")),
    ("October 20 at noon", ("2026-10-20", "12:00")),
    # military time must never become a year
    ("20th October 1400", ("2026-10-20", "14:00")),
    ("20th October 1400 hours", ("2026-10-20", "14:00")),
    ("October 21 at 14 hundred hours", ("2026-10-21", "14:00")),
    ("October 21 at fourteen hundred", ("2026-10-21", "14:00")),
    ("20 October 2026 at 2", ("2026-10-20", "14:00")),
    ("20 October 2027", ("2027-10-20", None)),
])
def test_full_phrases(text, expected):
    assert parse(text) == expected


# --------------------------------------------------- short answers to a question
@pytest.mark.parametrize("text, expecting, expected", [
    ("20", "date", ("2026-10-20", None)),
    ("the 21st", "date", ("2026-10-21", None)),
    ("twenty first", "date", ("2026-10-21", None)),
    ("10 30", "time", (None, "10:30")),
    ("1030", "time", (None, "10:30")),
    ("2 30", "time", (None, "14:30")),
    ("two thirty", "time", (None, "14:30")),
    ("2", "time", (None, "14:00")),
    ("11", "time", (None, "11:00")),
    ("10.30", "time", (None, "10:30")),
    ("1400", "time", (None, "14:00")),
    ("at 3 please", "time", (None, "15:00")),
])
def test_short_answers(text, expecting, expected):
    assert parse(text, expecting) == expected


# ------------------------------------------------------- things that are NOT times
@pytest.mark.parametrize("text, expecting", [
    ("My booking ID is BK24017", None),
    ("booking number 24017", None),
    ("1030", None),                 # a bare number is only a time when we asked for one
    ("20", None),                   # ... and only a date when we asked for a date
    ("10 30", None),
    ("my name is Karan Mehta", "time"),
    ("yes please", "time"),
    ("the first one", "time"),
])
def test_not_dates_or_times(text, expecting):
    assert parse(text, expecting) == (None, None)


def test_invalid_values_are_rejected():
    assert parse("October 32nd") == (None, None)
    assert parse("the 31st", "date")[0] == "2026-10-31"
    assert parse("at 25 30", "time") == (None, None)
    assert parse("October 20 at 10:75") == ("2026-10-20", None)


def test_legacy_allow_bare_time_keyword_still_works():
    _, digits = nlu.prepare("2")
    assert nlu.parse_datetime(digits, TODAY, allow_bare_time=True) == (None, "14:00")
