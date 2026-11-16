"""One structured "story" per synthetic loan. Every document is rendered from it, so labels are exact.

Banks are fictional, but each one's loan mix and size come from a real failed bank's last public Call
Report (data/public/loan_mix/<cert>.json). Everything about people and properties is invented (Faker).
"""
from __future__ import annotations

import json
import math
import random
import zlib
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

from faker import Faker

ROOT = Path(__file__).resolve().parents[2]
LOAN_MIX = ROOT / "data" / "public" / "loan_mix"

# Fictional banks. model_cert = the real failed bank whose public loan mix this portfolio copies.
# style = document layout + note-writing style. Test banks use styles never seen in training.
BANKS = [
    {"bank_id": "B1", "name": "Lakeside Community Bank",   "model_cert": 34578, "style": "S1", "split": "train"},
    {"bank_id": "B2", "name": "Pine Ridge Savings Bank",   "model_cert": 35065, "style": "S1", "split": "train"},
    {"bank_id": "B3", "name": "First Harbor National Bank", "model_cert": 19040, "style": "S2", "split": "train"},
    {"bank_id": "B4", "name": "Prairie Gate Bank",          "model_cert": 58052, "style": "S3", "split": "val"},
    {"bank_id": "B5", "name": "Copper Valley Bank, FSB",    "model_cert": 58495, "style": "S4", "split": "test"},
    {"bank_id": "B6", "name": "Northfield State Bank",      "model_cert": 23306, "style": "S5", "split": "test"},
    # Added 2026-11-11 as a FINAL holdout: scored once, after all rules and thresholds are frozen.
    {"bank_id": "B7", "name": "Redstone Federal Savings",   "model_cert": 25744, "style": "S6", "split": "holdout",
     "text_style": "S4"},   # notes reuse a test (never-trained) writing style; B7 is for extraction (U1)
    # Second final holdout (2026-11-11), scored once: new wording, slash day-first dates, hard packets.
    {"bank_id": "B8", "name": "Clearwater Bank of Commerce", "model_cert": 58052, "style": "S7", "split": "holdout2",
     "text_style": "S5"},
    # Third holdout (2026-11-11): the clean final number, after holdouts 1-2 informed the model choice.
    {"bank_id": "B9", "name": "Stonebridge Thrift Bank", "model_cert": 34578, "style": "S8", "split": "holdout3",
     "text_style": "S4"},
]
CLOSING_DATE = date(2010, 12, 17)   # the modeled banks' real closing date (all six closed the same day)

LOAN_TYPES = {  # Call Report field → loan type
    "LNRERES": "residential_1_4", "LNRENRES": "cre", "LNRECONS": "construction", "LNCI": "c_and_i", "LNCON": "consumer",
}
FALLBACK_MIX = {"residential_1_4": 0.25, "cre": 0.35, "construction": 0.2, "c_and_i": 0.15, "consumer": 0.05}

# Term, rate and size ranges by loan type (2004–2008 originations)
TERMS = {
    "residential_1_4": dict(years=(15, 30), rate=(5.25, 7.75), amount=(90_000, 650_000), io=False),
    "cre":             dict(years=(5, 10),  rate=(6.0, 8.5),   amount=(400_000, 4_500_000), io=False),
    "construction":    dict(years=(1, 3),   rate=(6.5, 9.0),   amount=(300_000, 3_500_000), io=True),
    "c_and_i":         dict(years=(3, 7),   rate=(6.5, 9.5),   amount=(75_000, 1_500_000), io=False),
    "consumer":        dict(years=(3, 6),   rate=(7.5, 12.0),  amount=(8_000, 60_000), io=False),
}
SECURED = {"residential_1_4", "cre", "construction"}   # have a deed of trust + appraisal

# Planted conditions: probability by loan type (text flags + cross-checks)
FLAG_P = {
    "bankruptcy":            {"default": 0.06},
    "litigation":            {"default": 0.04, "cre": 0.07, "construction": 0.08},
    "hardship":              {"default": 0.14, "residential_1_4": 0.22},
    "fraud_suspected":       {"default": 0.02},
    "property_damage":       {"default": 0.04, "consumer": 0.0, "c_and_i": 0.0},
    "payoff_request":        {"default": 0.05},
    "underwriting_exception": {"default": 0.18, "construction": 0.30},
    "draw_without_inspection": {"default": 0.0, "construction": 0.30},
}
DECOY_P = 0.12   # a sentence that mentions a flag's topic but does NOT mean the flag (negatives)


@dataclass
class Loan:
    loan_id: str
    bank_id: str
    loan_number: str
    loan_type: str
    borrower_name: str
    property_address: str | None
    note_date: str
    original_principal: float
    note_rate_pct: float
    term_months: int
    maturity_date: str
    monthly_payment: float
    interest_only: bool
    appraised_value: float | None
    stated_income: float | None
    verified_income: float | None
    dti_stated_pct: float | None
    current_upb: float
    next_due_date: str
    days_delinquent: int
    risk_rating: str
    flags: list[str] = field(default_factory=list)
    decoys: list[str] = field(default_factory=list)
    data_room: dict = field(default_factory=dict)        # servicing-system values (may disagree on purpose)
    data_room_mismatch: list[str] = field(default_factory=list)


def loan_mix(cert: int) -> tuple[dict[str, float], float]:
    """Share of each loan type and total net loans ($ thousands) from the bank's last Call Report."""
    p = LOAN_MIX / f"{cert}.json"
    if not p.exists():
        return FALLBACK_MIX, 0.0
    d = json.loads(p.read_text())
    amounts = {t: float(d.get(f) or 0) for f, t in LOAN_TYPES.items()}
    total = sum(amounts.values())
    if total <= 0:
        return FALLBACK_MIX, 0.0
    return {t: a / total for t, a in amounts.items()}, float(d.get("LNLSNET") or total)


def payment(principal: float, rate_pct: float, months: int, interest_only: bool) -> float:
    r = rate_pct / 100 / 12
    if interest_only:
        return round(principal * r, 2)
    return round(principal * r / (1 - (1 + r) ** -months), 2)


def balance_after(principal: float, rate_pct: float, months: int, paid: int, interest_only: bool) -> float:
    if interest_only:
        return round(principal, 2)
    r = rate_pct / 100 / 12
    pmt = principal * r / (1 - (1 + r) ** -months)
    bal = principal * (1 + r) ** paid - pmt * ((1 + r) ** paid - 1) / r
    return round(max(bal, 0.0), 2)


def st_closing_minus(months: int) -> date:
    return add_months(CLOSING_DATE, -months)


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    day = min(d.day, 28)
    return date(d.year + y, m + 1, day)


def months_between(a: date, b: date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month)


def _p(flag: str, loan_type: str) -> float:
    t = FLAG_P[flag]
    return t.get(loan_type, t["default"])


def make_loan(rng: random.Random, fake: Faker, bank: dict, n: int, loan_type: str) -> Loan:
    t = TERMS[loan_type]
    years = rng.randint(*t["years"])
    months = years * 12
    # Originated 2004–2009, but short loans mostly still outstanding (or recently matured) at the failure date
    earliest = max(date(2004, 1, 1), st_closing_minus(months + 18))
    note_d = earliest + timedelta(days=rng.randint(0, max(30, (date(2009, 9, 30) - earliest).days)))
    amount = round(rng.uniform(*t["amount"]) / 1000) * 1000
    rate = round(rng.uniform(*t["rate"]) * 8) / 8          # eighths, like real notes
    io = t["io"]
    pmt = payment(amount, rate, months, io)
    maturity = add_months(note_d, months)

    # Delinquency: a failing bank's book is stressed
    days_del = rng.choices([0, 30, 60, 90, 120, 180, 360], weights=[55, 12, 8, 7, 6, 6, 6])[0]
    missed = days_del // 30
    as_of = CLOSING_DATE
    paid = max(0, min(months, months_between(note_d, as_of) - missed))
    upb = balance_after(amount, rate, months, paid, io)
    next_due = add_months(note_d, paid + 1)

    secured = loan_type in SECURED
    ltv0 = rng.uniform(0.6, 0.95)
    appraised = round(amount / ltv0 / 1000) * 1000 if secured else None
    address = fake.address().replace("\n", ", ") if secured else None

    stated_income = verified_income = dti = None
    if loan_type in ("residential_1_4", "consumer"):
        verified_income = round(rng.uniform(35_000, 220_000) / 100) * 100
        stated_income = verified_income
        dti = round(rng.uniform(22, 45), 1)

    flags = [f for f in FLAG_P if rng.random() < _p(f, loan_type)]
    if days_del >= 90 and "hardship" not in flags and rng.random() < 0.5:
        flags.append("hardship")
    if "underwriting_exception" in flags and stated_income:
        # the classic 2006 story: stated income well above what the W-2 shows
        stated_income = round(verified_income * rng.uniform(1.3, 1.8) / 100) * 100
        flags.append("income_mismatch")
    decoys = [f for f in ("bankruptcy", "litigation", "fraud_suspected") if f not in flags and rng.random() < DECOY_P]

    rating = "pass"
    if days_del >= 90 or "bankruptcy" in flags:
        rating = rng.choice(["substandard", "doubtful"])
    elif days_del >= 30 or "hardship" in flags:
        rating = "special_mention"

    # Data-room schedule (servicing system): mostly agrees; a few planted disagreements to reconcile
    data_room = {"original_principal": amount, "note_rate_pct": rate, "maturity_date": maturity.isoformat(),
                 "current_upb": upb}
    mismatch = []
    r = rng.random()
    if r < 0.03:
        data_room["note_rate_pct"] = round(rate + rng.choice([-0.5, 0.25, 0.5, 1.0]), 3); mismatch.append("note_rate_pct")
    elif r < 0.07:
        data_room["current_upb"] = round(upb + pmt * rng.choice([1, 2]), 2); mismatch.append("current_upb")
    elif r < 0.09:
        data_room["maturity_date"] = add_months(maturity, rng.choice([12, 24])).isoformat(); mismatch.append("maturity_date")

    return Loan(
        loan_id=f"{bank['bank_id']}-{n:04d}", bank_id=bank["bank_id"],
        loan_number=f"{rng.randint(10, 99)}{rng.randint(1000000, 9999999)}",
        loan_type=loan_type, borrower_name=fake.name() if loan_type in ("residential_1_4", "consumer") else fake.company(),
        property_address=address, note_date=note_d.isoformat(), original_principal=float(amount),
        note_rate_pct=rate, term_months=months, maturity_date=maturity.isoformat(), monthly_payment=pmt,
        interest_only=io, appraised_value=appraised, stated_income=stated_income, verified_income=verified_income,
        dti_stated_pct=dti, current_upb=upb, next_due_date=next_due.isoformat(), days_delinquent=days_del,
        risk_rating=rating, flags=flags, decoys=decoys, data_room=data_room, data_room_mismatch=mismatch,
    )


def make_bank_loans(bank: dict, n_loans: int, seed: int) -> list[Loan]:
    rng = random.Random(f"{seed}-{bank['bank_id']}")
    fake = Faker("en_US")
    fake.seed_instance(zlib.crc32(f"{seed}-{bank['bank_id']}".encode()))   # hash() is salted per process
    mix, _ = loan_mix(bank["model_cert"])
    types, weights = zip(*sorted(mix.items()))
    # at least one of each type that exists in the mix, the rest by weight
    present = [t for t, w in zip(types, weights) if w > 0.01]
    picks = present + rng.choices(types, weights=weights, k=max(0, n_loans - len(present)))
    return [make_loan(rng, fake, bank, i + 1, t) for i, t in enumerate(picks[:n_loans])]


def to_json(loan: Loan) -> dict:
    return asdict(loan)


if __name__ == "__main__":
    for b in BANKS:
        loans = make_bank_loans(b, 5, seed=7)
        print(b["bank_id"], [(l.loan_type, l.flags) for l in loans][:3])
        assert not math.isnan(loans[0].current_upb)
