"""Deterministic language understanding for transcribed speech.

Rules rather than an LLM keep slot extraction and yes/no detection predictable,
which matters when a wrong "yes" would change a booking.
"""

from __future__ import annotations

import difflib
import re
from .temporal import MONTHS, WEEKDAYS, parse_date, parse_datetime, parse_time  # noqa: F401  (re-exported)

UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50}
ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
    "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
    "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19,
    "twentieth": 20, "thirtieth": 30,
}


NAME_STOPWORDS = {
    "and", "i", "i'd", "i'm", "want", "would", "like", "to", "for", "book", "booking", "on", "at", "please",
    "calling", "here", "looking", "a", "an", "the", "my", "need", "with", "from", "is", "it", "speaking",
    "yes", "no", "okay", "ok", "sure", "hi", "hello", "thanks", "thank", "you", "appointment", "cancel",
}
FILLERS = r"^(?:(?:um+|uh+|hmm+|okay|ok|sure|yes|yeah|so|well|hi|hello|it's|it is|that's|that is|i'm|i am|"\
          r"my name is|name is|this is|the name is|its)\b[\s,]*)+"


# --------------------------------------------------------------------------- text
def normalize(text: str) -> str:
    t = text.lower()
    t = re.sub(r"\b([ap])\.\s?m\.?", r"\1m", t)          # a.m. -> am
    t = re.sub(r"\b([ap])\s+m\b", r"\1m", t)               # a m -> am
    t = t.replace("o'clock", " oclock").replace("’", "'")
    t = re.sub(r"(?<=[a-z])-(?=[a-z])", " ", t)            # check-up -> check up
    t = re.sub(r"(?<=\d),(?=\d)", "", t)                   # 24,017 -> 24017
    t = re.sub(r"[^a-z0-9:/'\-\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def words_to_digits(t: str) -> str:
    """'twenty first' -> '21th', 'ten' -> '10', 'two four' -> '2 4'."""
    tokens = t.split()
    out: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if tok in TENS and nxt in UNITS and 0 < UNITS[nxt] < 10:
            out.append(str(TENS[tok] + UNITS[nxt])); i += 2; continue
        if tok in TENS and nxt in ORDINALS and ORDINALS[nxt] < 10:
            out.append(f"{TENS[tok] + ORDINALS[nxt]}th"); i += 2; continue
        if tok in UNITS:
            out.append(str(UNITS[tok]))
        elif tok in TENS:
            out.append(str(TENS[tok]))
        elif tok in ORDINALS:
            out.append(f"{ORDINALS[tok]}th")
        else:
            out.append(tok)
        i += 1
    return " ".join(out)


def prepare(text: str) -> tuple[str, str]:
    """Return (normalized, normalized-with-numbers-as-digits)."""
    n = normalize(text)
    return n, words_to_digits(n)


def parse_service(t: str, services: list[dict]) -> str | None:
    keywords = sorted(((kw.lower(), s["service"]) for s in services for kw in s["keywords"] + [s["service"]]),
                      key=lambda kv: len(kv[0]), reverse=True)
    for kw, service in keywords:
        if re.search(rf"\b{re.escape(kw)}\b", t):
            return service
    # Fuzzy fallback for STT near-misses such as "hair cutt" or "physio therapy".
    tokens = t.split()
    grams = tokens + [" ".join(tokens[i:i + 2]) for i in range(len(tokens) - 1)]
    best, best_score = None, 0.0
    for gram in grams:
        if len(gram) < 5:
            continue
        for kw, service in keywords:
            if len(kw) < 5:
                continue
            score = difflib.SequenceMatcher(None, gram, kw).ratio()
            if score > best_score:
                best, best_score = service, score
    return best if best_score >= 0.82 else None


def _clean_name(tokens: list[str]) -> str | None:
    picked = []
    for tok in tokens:
        bare = tok.strip(".,!?").lower()
        if not bare or bare in NAME_STOPWORDS or not re.fullmatch(r"[a-z][a-z'\-]*", bare):
            break
        picked.append(tok.strip(".,!?"))
        if len(picked) == 3:
            break
    return " ".join(w[:1].upper() + w[1:].lower() for w in picked) or None


def parse_name(raw: str, expecting: bool, blocked_words: set[str] | None = None) -> str | None:
    """Extract a customer name from the original-case transcript."""
    blocked_words = blocked_words or set()
    strong = re.search(r"\b(?:my name is|my name's|name is|call me|the name is)\s+(.+)", raw, re.I)
    if strong:
        name = _clean_name(strong[1].split())
        if name:
            return name
    weak = re.search(r"\b(?i:i am|i'm|this is|it's)\s+([A-Z][a-zA-Z'\-]+(?:\s+[A-Z][a-zA-Z'\-]+){0,2})", raw)
    if weak:
        name = _clean_name(weak[1].split())
        if name and name.split()[0].lower() not in blocked_words:
            return name
    if expecting:
        stripped = re.sub(FILLERS, "", raw.strip(), flags=re.I).strip(" .,!?")
        tokens = stripped.split()
        if 1 <= len(tokens) <= 3 and all(re.fullmatch(r"[A-Za-z][A-Za-z'\-]*[.,]?", tok) for tok in tokens):
            if not any(tok.strip(".,").lower() in blocked_words | NAME_STOPWORDS for tok in tokens):
                return _clean_name(tokens)
    return None


_YES = r"\b(yes|yeah|yep|yup|ya|yea|sure|sure thing|confirm|confirmed|correct|right|that's right|ok|okay|"\
       r"absolutely|definitely|perfect|affirmative|go ahead|go for it|please do|do it|book it|that works|"\
       r"sounds good|that's fine|fine|haan|han|haa|ji|ji haan|theek hai|thik hai|bilkul|kar do)\b"
_NO = r"\b(no|nope|nah|not|don't|dont|do not|never mind|nevermind|negative|wrong|stop|wait|hold on|nahi|nahin|nai|na|mat)\b"


def parse_yes_no(t: str, context: str) -> bool | None:
    """True/False for an explicit answer, None when ambiguous (never treated as consent)."""
    no = re.search(_NO, t) is not None
    if context == "book" and re.search(r"\b(cancel|different|another|change)\b", t):
        no = True
    if context == "cancel" and re.search(r"\b(keep|don't cancel|do not cancel)\b", t):
        no = True
    yes = re.search(_YES, t) is not None
    if context == "cancel" and re.search(r"\bcancel (it|that|the booking|my booking|please)\b", t):
        yes = True
    if no:
        return False
    if yes:
        return True
    return None


def parse_booking_id(t_digits: str, expecting: bool) -> str | None:
    patterns = [
        r"\b(?:b\s*k|bee\s*kay|bk)[\s\-:]*((?:\d[\s\-]*){5})(?!\d)",
        r"\b(?:booking|reference|confirmation)\s*(?:id|number|code|no)?\s*(?:is\s*)?((?:\d[\s\-]*){5})(?!\d)",
    ]
    if expecting:
        patterns.append(r"(?<!\d)((?:\d[\s\-]*){5})(?!\d)")
    for pattern in patterns:
        m = re.search(pattern, t_digits)
        if m:
            return "BK" + re.sub(r"\D", "", m[1])
    return None


def parse_intent(t: str) -> str | None:
    if re.search(r"\b(cancel|cancellation|call off)\b", t):
        return "cancel"
    if re.search(r"\b(book|booking|appointment|schedule|reserve|reservation|slot)\b", t):
        return "book"
    return None


def parse_option_choice(t: str, count: int) -> int | None:
    """Pick from an offered list: 'the first one', 'option two', 'the last one'."""
    m = re.search(r"\b(?:option|number|choice)\s+(one|two|three|1|2|3)\b", t)
    if m:
        idx = {"one": 0, "two": 1, "three": 2, "1": 0, "2": 1, "3": 2}[m[1]]
        return idx if idx < count else None
    m = re.search(r"\b(first|1st|second|2nd|third|3rd|last|earliest)\b", t)
    if m:
        idx = {"first": 0, "1st": 0, "earliest": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2}.get(m[1], count - 1)
        return idx if idx < count else None
    return None


def is_reset(t: str) -> bool:
    return re.search(r"\b(start over|restart|reset|begin again)\b", t) is not None
