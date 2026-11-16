import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import route  # noqa: E402


def row(field, value, raw=None, outcome="correct", ambiguous="False"):
    return {"loan_id": "X-0001", "doc_type": "promissory_note", "field": field, "value": value, "raw": raw or str(value),
            "outcome": outcome, "ambiguous": ambiguous, "confidence": 0.9}


def test_swapped_principal_and_payment_is_flagged():
    by_doc = {("X-0001", "promissory_note"): {"original_principal": "21436.46", "monthly_payment": "1051000.0"}}
    assert route.rule_fail(row("original_principal", "21436.46"), by_doc)
    assert route.rule_fail(row("monthly_payment", "1051000.0"), by_doc)


def test_plausible_payment_passes():
    by_doc = {("X-0001", "promissory_note"): {"original_principal": "1051000.0", "monthly_payment": "8452.13"}}
    assert not route.rule_fail(row("monthly_payment", "8452.13"), by_doc)


def test_partial_date_goes_to_review():
    assert route.rule_fail(row("next_due_date", "2011-01-01", raw="Jan-2011"), {})
    assert not route.rule_fail(row("next_due_date", "2011-01-07", raw="01/07/2011"), {})
    assert not route.rule_fail(row("next_due_date", "2011-01-07", raw="January 7, 2011"), {})


def test_ambiguous_day_month_goes_to_review():
    assert route.rule_fail(row("note_date", "2008-04-06", raw="06.04.2008", ambiguous="True"), {})


def test_missing_value_goes_to_review():
    assert route.rule_fail(row("note_rate_pct", None, outcome="missing"), {})


def test_rate_out_of_range():
    assert route.rule_fail(row("note_rate_pct", "62.5"), {})
