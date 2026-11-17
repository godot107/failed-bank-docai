"""Candidate loan-level rules (offline only; not wired into route.py, SQL or the review app).

These check a loan's values against each other the way a loan reviewer would, across documents:

  amortization   the note's payment matches principal, rate and term: fully amortizing over the term, interest-only,
                 or amortizing over 15/20/25/30 years with a balloon at maturity. Tolerance 1% or $1.
  term           maturity minus note date is a whole number of years (±1 month), between 1 and 40.
  closing_dates  the note is dated before the bank closed (and after 1990); the next payment is due after the
                 note date and no more than ~2 months after the closing.
  cross_document the servicing balance isn't above the note's principal (+2%): one value is misread, typically the
                 payment read as the principal. Works even when the payment wasn't read. Same idea with the
                 appraisal: loan-to-value from the note's own principal (production uses the bank's schedule) 30-160%.
  balance        the servicing balance is within a band of the balance the note's schedule implies at the next due
                 date: not more than 2% above (capitalized interest, modification → a person should know), not more
                 than 25% below (more than any normal prepayment → likely a misread).

A failed rule sends every value it used to review: the reviewer looks at one note page and the servicing screen.
Usage: see tools/measure_rules_v2.py.
"""
from __future__ import annotations

from datetime import date

BALLOON_AMORT_MONTHS = (180, 240, 300, 360)
FIELDS = ("note_date", "original_principal", "note_rate_pct", "monthly_payment", "maturity_date", "current_upb",
          "next_due_date", "appraised_value")
DATES = ("note_date", "maturity_date", "next_due_date")


def parse(field: str, v) -> float | date | None:
    if v in (None, "", "None"):
        return None
    try:
        return date.fromisoformat(str(v)[:10]) if field in DATES else float(v)
    except ValueError:
        return None


def months_between(a: date, b: date) -> float:
    """Calendar months from a to b, with the day difference as a fraction (note 03-14 → maturity 03-14 = whole)."""
    return (b.year - a.year) * 12 + (b.month - a.month) + (b.day - a.day) / 30.44


def amortizing_payment(principal: float, rate_pct: float, months: int) -> float:
    r = rate_pct / 100 / 12
    return principal / months if r == 0 else principal * r / (1 - (1 + r) ** -months)


def balance_after(principal: float, rate_pct: float, amort_months: int, paid: int) -> float:
    r = rate_pct / 100 / 12
    pmt = amortizing_payment(principal, rate_pct, amort_months)
    return max(principal * (1 + r) ** paid - pmt * ((1 + r) ** paid - 1) / r, 0.0) if r else principal - pmt * paid


def payment_basis(v: dict) -> tuple[str, int] | None:
    """('io', 0) or ('amortizing', months) that explains the note's payment; None if nothing does."""
    p, rate, pmt, n = v.get("original_principal"), v.get("note_rate_pct"), v.get("monthly_payment"), v.get("_term")
    if not (p and rate and pmt and n and n > 0):
        return None
    close = lambda x: abs(pmt - x) <= max(1.0, 0.01 * x)       # noqa: E731
    if close(p * rate / 100 / 12):
        return ("io", 0)
    for m in (round(n), *BALLOON_AMORT_MONTHS):
        if m > 0 and close(amortizing_payment(p, rate, m)):
            return ("amortizing", m)
    return None


def loan_problems_v2(values: dict, closing: date) -> dict[str, str]:
    """field → reason for every value a candidate rule sends to review. `values` = one loan's values, any doc."""
    v = {f: parse(f, values.get(f)) for f in FIELDS}
    out: dict[str, str] = {}

    def flag(fields, why):
        for f in fields:
            if v.get(f) is not None:
                out.setdefault(f, why)

    note, mat, due = v["note_date"], v["maturity_date"], v["next_due_date"]
    if note and mat and mat > note:
        v["_term"] = months_between(note, mat)
        n = v["_term"]
        if not (11 <= n <= 481) or abs(n - 12 * round(n / 12)) > 1:
            flag(("note_date", "maturity_date"), f"term: {n:.1f} months is not a whole number of years")
    if all(v[f] for f in ("original_principal", "note_rate_pct", "monthly_payment")) and v.get("_term"):
        if payment_basis(v) is None:
            flag(("note_date", "original_principal", "note_rate_pct", "monthly_payment", "maturity_date"),
                 "amortization: payment doesn't match principal, rate and term (amortizing, interest-only or balloon)")
    if note and not (date(1990, 1, 1) <= note < closing):
        flag(("note_date",), "closing_dates: note dated after the bank closed or before 1990")
    if due and note and not (note < due <= date.fromordinal(closing.toordinal() + 62)):
        flag(("next_due_date",), "closing_dates: next due date not between the note date and ~2 months after closing")
    upb = v["current_upb"]
    if upb is not None and v["original_principal"] and upb > v["original_principal"] * 1.02 + 1:
        flag(("original_principal", "current_upb"),
             "cross_document: servicing balance is above the principal on the note")
    if v["original_principal"] and v["appraised_value"]:
        ltv = v["original_principal"] / v["appraised_value"]
        if not 0.3 <= ltv <= 1.6:
            flag(("original_principal", "appraised_value"),
                 f"cross_document: loan-to-value {ltv:.0%} from the note's principal and the appraisal is implausible")
    basis = payment_basis(v)
    if upb is not None and basis and due and note and due > note:
        paid = max(0, round(months_between(note, due)) - 1)
        sched = v["original_principal"] if basis[0] == "io" else \
            balance_after(v["original_principal"], v["note_rate_pct"], basis[1], paid)
        if upb > sched * 1.02 + 1:
            flag(("current_upb",), f"balance: {upb:,.0f} is above the scheduled {sched:,.0f}")
        elif upb < sched * 0.75 - 1:
            flag(("current_upb",), f"balance: {upb:,.0f} is far below the scheduled {sched:,.0f}")
    return out
