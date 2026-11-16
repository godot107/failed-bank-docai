"""Manual-keying parser and the answer key used for the AI-vs-manual comparison."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from keying import parse  # noqa: E402
from turnaround import truth_rows  # noqa: E402


def test_money_and_rates():
    assert parse("original_principal", "$1,234,500.00") == 1234500.0
    assert parse("note_rate_pct", "8.625%") == 8.625
    assert parse("monthly_payment", "  ") is None


def test_dates_all_formats_same_number():
    for s in ("2006-03-14", "3/14/2006", "20060314"):
        assert parse("note_date", s) == 20060314.0


@pytest.mark.parametrize("field,text", [("note_date", "2006-02-30"), ("note_date", "March 14"),
                                        ("current_upb", "about 5k")])
def test_bad_input_is_rejected_not_stored(field, text):
    with pytest.raises(ValueError):
        parse(field, text)


def test_answer_key_blank_when_not_printed():
    labels = sorted((Path(__file__).resolve().parents[1] / "samples/synthetic/labels").glob("*.json"))
    loan = labels[0].stem
    rows = truth_rows([f"{loan}-PN", f"{loan}-SH", "not-a-doc"])
    assert rows and all(r[0].startswith(loan) for r in rows)
    for doc_id, field, value, printed in rows:
        assert printed or value is None      # a value that isn't on the page must be keyed as blank
