from datetime import date

from rules_v2 import amortizing_payment, loan_problems_v2

CLOSING = date(2010, 12, 17)
P, RATE = 200_000.0, 6.5
PMT = round(amortizing_payment(P, RATE, 360), 2)
GOOD = {"note_date": "2006-03-14", "original_principal": P, "note_rate_pct": RATE, "monthly_payment": PMT,
        "maturity_date": "2036-03-14", "next_due_date": "2011-01-14", "appraised_value": 250_000.0}


def with_(**kw):
    return {**GOOD, **kw}


def scheduled_upb():
    from rules_v2 import balance_after
    return round(balance_after(P, RATE, 360, 57), 2)


def test_clean_loan_passes():
    assert loan_problems_v2(with_(current_upb=scheduled_upb()), CLOSING) == {}


def test_interest_only_and_balloon_pass():
    io = with_(monthly_payment=round(P * RATE / 1200, 2), maturity_date="2009-03-14", next_due_date="2009-01-14",
               current_upb=P)
    assert "monthly_payment" not in loan_problems_v2(io, CLOSING)
    balloon = with_(maturity_date="2013-03-14")        # 7-year balloon, 30-year amortization
    assert "monthly_payment" not in loan_problems_v2(balloon, CLOSING)


def test_rate_misread_fails_amortization():
    assert loan_problems_v2(with_(note_rate_pct=8.5), CLOSING)["note_rate_pct"].startswith("amortization")


def test_odd_term_and_dates():
    p = loan_problems_v2(with_(maturity_date="2036-09-14"), CLOSING)
    assert p["maturity_date"].startswith(("term", "amortization"))
    assert loan_problems_v2(with_(note_date="2011-03-14", maturity_date="2041-03-14"), CLOSING)["note_date"]
    assert loan_problems_v2(with_(next_due_date="2012-01-14"), CLOSING)["next_due_date"].startswith("closing_dates")


def test_payment_read_as_principal_caught_without_payment():
    v = with_(original_principal=PMT, current_upb=scheduled_upb())
    del v["monthly_payment"]
    assert loan_problems_v2(v, CLOSING)["original_principal"].startswith("cross_document")


def test_balance_off_schedule():
    assert loan_problems_v2(with_(current_upb=scheduled_upb() * 1.05), CLOSING)["current_upb"].startswith("balance")
    assert loan_problems_v2(with_(current_upb=scheduled_upb() / 10), CLOSING)["current_upb"].startswith("balance")
