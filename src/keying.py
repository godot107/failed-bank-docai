"""Parse values a person keyed by hand on the /manual page into the review app's numeric encoding.

Money and rates: "1,234.50", "$1234.5", "8.625%" -> float. Dates: "2006-03-14", "3/14/2006", "20060314" -> 20060314.0
(YYYYMMDD as a number, like review.item). Blank -> None ("not on the document"). Anything else -> ValueError, so the
page can ask again instead of storing a wrong value. No dependencies: the web app imports it as is.
"""
from __future__ import annotations

import re
from datetime import date

LABELS = {
    "note_date": "Note date",
    "original_principal": "Original principal ($)",
    "note_rate_pct": "Note rate (%)",
    "monthly_payment": "Monthly payment ($)",
    "maturity_date": "Maturity date",
    "current_upb": "Current unpaid balance ($)",
    "next_due_date": "Next due date",
    "appraised_value": "Appraised value ($)",
}


def is_date(field: str) -> bool:
    return field.endswith("_date")


def parse(field: str, text: str | None) -> float | None:
    s = (text or "").strip()
    if not s:
        return None
    if is_date(field):
        m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s) or re.fullmatch(r"(\d{4})(\d{2})(\d{2})", s)
        if m:
            y, mo, d = (int(x) for x in m.groups())
        else:
            m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
            if not m:
                raise ValueError(f"{LABELS.get(field, field)}: enter a date like 2006-03-14 or 3/14/2006")
            mo, d, y = (int(x) for x in m.groups())
        try:
            date(y, mo, d)
        except ValueError:
            raise ValueError(f"{LABELS.get(field, field)}: {s} is not a real date") from None
        return float(f"{y:04d}{mo:02d}{d:02d}")
    t = s.replace(",", "").replace("$", "").replace("%", "").replace("USD", "").strip()
    if not re.fullmatch(r"-?\d+(\.\d+)?", t):
        raise ValueError(f"{LABELS.get(field, field)}: enter a number like 1,234.56")
    return float(t)
