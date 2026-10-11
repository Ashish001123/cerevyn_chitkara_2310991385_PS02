"""Spoken date and time grammar.

Input is text already normalized by `nlu.prepare` (lowercase, punctuation
stripped, number words turned into digits, e.g. "twenty first" -> "21th").

Two rules keep this from guessing:
- a bare number ("20", "1030") is only read as a date or time when the agent
  has just asked for one (`expecting`), or when a date in the same sentence
  makes a following number clearly a time;
- a four-digit number is a year only if it is this year or next; otherwise it
  is left for the time grammar ("20th October 1400" -> 14:00, not year 1400).
"""

from __future__ import annotations

import re
from datetime import date, timedelta

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4,
    "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9,
    "sept": 9, "sep": 9, "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

_MONTH = "|".join(sorted(MONTHS, key=len, reverse=True))
_WEEKDAY = "|".join(WEEKDAYS)
_ORD = r"(?:st|nd|rd|th)"
_NOT_OPTION = r"(?!\s+(?:1|one|option)\b)"

Span = tuple[int, int]

def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _upcoming(month: int, day: int, today: date) -> date | None:
    """Month and day without a year: this year, or next year if already past."""
    d = _safe_date(today.year, month, day)
    if d and d >= today:
        return d
    return _safe_date(today.year + 1, month, day)


def _upcoming_day(day: int, today: date, next_month: bool = False) -> date | None:
    """Day of month without a month: the nearest upcoming month that has that day."""
    if not 1 <= day <= 31:
        return None
    year, month = today.year, today.month
    if next_month:
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    for _ in range(13):
        d = _safe_date(year, month, day)
        if d and d >= today:
            return d
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return None


def _year_after(t: str, end: int, today: date) -> tuple[int | None, int]:
    """An explicit year right after a date, only if it is plausible for a booking."""
    m = re.match(r",?\s+(\d{4})\b", t[end:])
    if m and today.year <= int(m[1]) <= today.year + 1:
        return int(m[1]), end + m.end()
    return None, end


def parse_date(t: str, today: date, expecting: str | None = None) -> tuple[date | None, Span | None]:
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", t)
    if m:
        d = _safe_date(int(m[1]), int(m[2]), int(m[3]))
        if d:
            return d, m.span()

    for pattern, day_group, month_group in (
        (rf"\b(\d{{1,2}}){_ORD}?\s+(?:of\s+)?({_MONTH})\b", 1, 2),
        (rf"\b({_MONTH})\s+(?:the\s+)?(\d{{1,2}}){_ORD}?\b", 2, 1),
    ):
        m = re.search(pattern, t)
        if m:
            month, day = MONTHS[m[month_group]], int(m[day_group])
            year, end = _year_after(t, m.end(), today)
            d = _safe_date(year, month, day) if year else _upcoming(month, day, today)
            if d:
                return d, (m.start(), end)
            return None, None 

    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", t)  # Indian dd/mm[/yyyy]
    if m:
        day, month = int(m[1]), int(m[2])
        if m[3]:
            d = _safe_date(int(m[3]) + (2000 if len(m[3]) == 2 else 0), month, day)
        else:
            d = _upcoming(month, day, today)
        if d:
            return d, m.span()

    for phrase, offset in (("day after tomorrow", 2), ("tomorrow", 1), ("today", 0)):
        m = re.search(rf"\b{phrase}\b", t)
        if m:
            return today + timedelta(days=offset), m.span()

    m = re.search(rf"\b(?:next\s+|this\s+|on\s+)?({_WEEKDAY})\b", t)
    if m:
        ahead = (WEEKDAYS.index(m[1]) - today.weekday()) % 7 or 7
        return today + timedelta(days=ahead), m.span()

    m = re.search(rf"\b(?:on\s+)?(?:the\s+)?(\d{{1,2}}){_ORD}\b{_NOT_OPTION}"
                  rf"(?:\s+of\s+(this|next|the)\s+month)?", t)
    if m:
        d = _upcoming_day(int(m[1]), today, next_month=m[2] == "next")
        return (d, m.span()) if d else (None, None)

    m = re.match(r"\s*(?:on\s+)?(?:the\s+)?(\d{1,2})(?=\s+at\s+\d)", t)
    if m:
        d = _upcoming_day(int(m[1]), today)
        if d:
            return d, m.span(1)

    if expecting == "date":  # answer to "which date?": "20", "the 21", "20 please"
        m = re.fullmatch(r"\s*(?:on\s+)?(?:the\s+)?(\d{1,2})(?:\s+please)?\s*", t)
        if m:
            d = _upcoming_day(int(m[1]), today)
            if d:
                return d, m.span(1)
    return None, None


def _business(hour: int, daypart: str | None) -> int:
    """Resolve a 12-hour clock reading. Appointments run 8 AM - 7 PM."""
    if daypart == "pm" and hour < 12:
        return hour + 12
    if daypart == "am":
        return 0 if hour == 12 else hour
    return hour + 12 if 1 <= hour <= 7 else hour


def _hhmm(hour: int, minute: int) -> str | None:
    return f"{hour:02d}:{minute:02d}" if 0 <= hour < 24 and 0 <= minute < 60 else None


def _twelve_hour(hour: int, minute: int, daypart: str | None) -> str | None:
    if not 1 <= hour <= 12:
        return None
    return _hhmm(_business(hour, daypart), minute)


def _clock_digits(hour: int, minute: int, daypart: str | None) -> str | None:
    """'1030' -> 10:30 (12-hour rules), '1400' -> 14:00 (24-hour)."""
    return _twelve_hour(hour, minute, daypart) if hour <= 12 else _hhmm(hour, minute)


def parse_time(t: str, allow_bare: bool = False, loose: bool | None = None) -> str | None:
    """HH:MM (24h) from text with any date already removed.

    allow_bare: the agent asked for a time, so "2" or "1030" alone is a time.
    loose: colon-free forms like "10 30" are accepted (defaults to allow_bare;
    the caller also sets it when the sentence contained a date).
    """
    loose = allow_bare if loose is None else loose
    daypart = ("pm" if re.search(r"\b(afternoon|evening)\b", t)
               else "am" if re.search(r"\bmorning\b", t) else None)

    m = re.search(r"\b(\d{1,2})(?:[:\s]?(\d{2}))?\s*(am|pm)\b", t)            # 10 am, 2 30 pm, 1030am
    if m and (r := _twelve_hour(int(m[1]), int(m[2] or 0), m[3])):
        return r
    if re.search(r"\b(noon|midday)\b", t):
        return "12:00"
    m = re.search(r"\b(quarter|half) past (\d{1,2})\b", t)
    if m and (r := _twelve_hour(int(m[2]), 15 if m[1] == "quarter" else 30, daypart)):
        return r
    m = re.search(r"\bquarter to (\d{1,2})\b", t)
    if m and 1 <= int(m[1]) <= 12:
        return _twelve_hour(int(m[1]) - 1 or 12, 45, daypart)
    m = re.search(r"\b(\d{1,2})\s*hundred\b", t)                              # 14 hundred (hours)
    if m:
        return _hhmm(int(m[1]), 0)
    m = re.search(r"\b(\d{2})(\d{2})\s*hours\b", t)                          # 1400 hours
    if m:
        return _hhmm(int(m[1]), int(m[2]))
    m = re.search(r"\b(\d{1,2}):(\d{2})\b", t)                               # 10:30, 14:00
    if m:
        hour, minute = int(m[1]), int(m[2])
        if minute >= 60:
            return None  
        return _clock_digits(hour, minute, daypart)
    m = re.search(r"\b(\d{1,2})\s*o\s?'?\s?clock\b", t)
    if m and (r := _twelve_hour(int(m[1]), 0, daypart)):
        return r
    m = re.search(r"\b(\d{1,2})\s+in the (morning|afternoon|evening)\b", t)
    if m and (r := _twelve_hour(int(m[1]), 0, "am" if m[2] == "morning" else "pm")):
        return r
    m = re.search(r"\bat\s+(\d{1,2})\s+(\d{2})\b", t)                         # at 2 30
    if m and (r := _twelve_hour(int(m[1]), int(m[2]), daypart)):
        return r
    m = re.search(r"\bat\s+(\d{1,2})(\d{2})\b", t)                            # at 1030
    if m and (r := _clock_digits(int(m[1]), int(m[2]), daypart)):
        return r
    if loose:
        m = re.search(r"(?<!\d)\b(\d{1,2})\s+(\d{2})\b(?!\s*\d)", t)          # 10 30
        if m and (r := _twelve_hour(int(m[1]), int(m[2]), daypart)):
            return r
        m = re.search(r"(?<!\d)\b(\d{1,2})(\d{2})\b(?!\s*\d)", t)            # 1030, 1400
        if m and (r := _clock_digits(int(m[1]), int(m[2]), daypart)):
            return r
    m = re.search(r"\bat\s+(\d{1,2})\b(?!\s*(?:st|nd|rd|th|\d))", t)          # at 10
    if m and (r := _twelve_hour(int(m[1]), 0, daypart)):
        return r
    if allow_bare:
        m = re.fullmatch(r"\s*(?:at\s+)?(\d{1,2})(?:\s+(?:please|in the (?:morning|afternoon|evening)))?\s*", t)
        if m and (r := _twelve_hour(int(m[1]), 0, daypart)):
            return r
    return None


def parse_datetime(t: str, today: date, allow_bare_time: bool = False,
                   expecting: str | None = None) -> tuple[date | None, str | None]:
    d, span = parse_date(t, today, expecting)
    rest = t if span is None else t[:span[0]] + " " + t[span[1]:]
    bare = allow_bare_time or expecting == "time"
    return d, parse_time(rest, allow_bare=bare, loose=bare or d is not None)
