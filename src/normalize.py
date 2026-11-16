"""Turn printed values ("USD 1,494,000.00", "8.625%", "14-Mar-2006", "Mar 14 2006") into canonical values.

Format-agnostic on purpose: the test banks print formats never seen in training, and normalization must not
be the thing that fails.
"""
from __future__ import annotations

import re
from datetime import datetime

from dateutil import parser as dparser

FIELD_TYPES = {
    "note_date": "date", "original_principal": "money", "note_rate_pct": "percent", "monthly_payment": "money",
    "maturity_date": "date", "current_upb": "money", "next_due_date": "date", "appraised_value": "money",
}

# Which fields each document type is responsible for (U1 extraction targets)
FIELDS_BY_DOC = {
    "promissory_note": ["note_date", "original_principal", "note_rate_pct", "monthly_payment", "maturity_date"],
    "servicing_history": ["current_upb", "next_due_date"],
    "appraisal_1004": ["appraised_value"],
}

NUM = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def money(s: str | None) -> float | None:
    if not s:
        return None
    m = NUM.search(s.replace(" ", ""))
    return round(float(m.group().replace(",", "")), 2) if m else None


def percent(s: str | None) -> float | None:
    if not s:
        return None
    m = NUM.search(s)
    if not m:
        return None
    v = float(m.group().replace(",", ""))
    return round(v, 4) if 0 < v < 30 else None


NUMERIC_DATE = re.compile(r"^\s*(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\s*$")


def date_order(raws: list[str]) -> str | None:
    """Infer a bank's numeric date order from its unambiguous dates: 'dmy', 'mdy', or None if never proven.
    A first number above 12 proves day-first; a second number above 12 proves month-first."""
    dmy = mdy = 0
    for r in raws:
        m = NUMERIC_DATE.match(r or "")
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            dmy += a > 12
            mdy += b > 12
    if dmy and not mdy:
        return "dmy"
    if mdy and not dmy:
        return "mdy"
    return None


def ambiguous_date(raw: str | None, order: str | None) -> bool:
    """A numeric date whose day and month could be swapped, at a bank whose order isn't proven."""
    m = NUMERIC_DATE.match(raw or "")
    return bool(m) and order is None and int(m.group(1)) <= 12 and int(m.group(2)) <= 12 and m.group(1) != m.group(2)


def as_date(s: str | None, dayfirst: bool = False) -> str | None:
    if not s:
        return None
    try:
        return dparser.parse(s, default=datetime(1900, 1, 1), dayfirst=dayfirst, fuzzy=True).date().isoformat()
    except (ValueError, OverflowError):
        return None


def normalize(field: str, s: str | None, date_order_: str | None = None):
    if FIELD_TYPES[field] == "date":
        return as_date(s, dayfirst=date_order_ == "dmy")
    return {"money": money, "percent": percent}[FIELD_TYPES[field]](s)


def same(field: str, got, truth) -> bool:
    if got is None or truth is None:
        return False
    t = FIELD_TYPES[field]
    if t == "money":
        return abs(float(got) - float(truth)) < 0.995      # S5 prints whole dollars
    if t == "percent":
        return abs(float(got) - float(truth)) < 0.0005
    return str(got) == str(truth)
