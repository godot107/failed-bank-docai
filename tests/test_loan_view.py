"""Loan view: packet-wide checks, the proposed sale pool, value formatting and schedule tolerances (no Azure)."""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from loan_view import fmt, packet_checks, sale_pool, same  # noqa: E402
from schemas import loan_problems  # noqa: E402


def checks(values):
    return {label: (result, detail) for label, _, result, detail in packet_checks(values, loan_problems(values))}


def test_swapped_payment_fails_one_rule_once():
    v = {"original_principal": "1051000", "monthly_payment": "1051000", "note_date": "2006-07-04",
         "maturity_date": "2011-07-04", "current_upb": "380589.55", "reference_principal": 1051000}
    c = checks(v)
    assert c["Payment vs principal (note)"][0] == "warn"
    assert "100.0%" in c["Payment vs principal (note)"][1]
    assert c["Date order (note → maturity)"][0] == "ok"
    assert c["Balance vs original amount (servicing history vs schedule)"][0] == "ok"
    assert len([1 for r, _ in c.values() if r == "warn"]) == 1      # the payment rule flags two fields, shown once


def test_cross_document_rules_pass_with_numbers():
    v = {"original_principal": "200000", "monthly_payment": "1500", "note_date": "2006-07-04",
         "maturity_date": "2036-07-04", "appraised_value": "250000", "reference_principal": 200000}
    c = checks(v)
    assert c["Loan-to-value (appraisal vs schedule)"] == ("ok", "loan-to-value 80% (expected 30%-160%)")
    assert c["Payment vs principal (note)"][1].startswith("payment is 0.8% of principal")


def test_date_order_and_out_of_range_rate():
    v = {"note_date": "2006-07-04", "maturity_date": "2005-01-01", "note_rate_pct": "40"}
    c = checks(v)
    assert c["Date order (note → maturity)"][0] == "warn"
    assert c["Note rate (%) value"][0] == "warn"                  # a single-value rule still appears


def test_rules_only_run_when_their_inputs_exist():
    assert packet_checks({"note_date": "2006-07-04"}, {}) == []


def test_sale_pool_from_extracted_next_due_date():
    closing = date(2010, 12, 17)
    assert sale_pool("c_and_i", "2010-01-04", closing) == (347, "Commercial & industrial · non-performing (90+ days)")
    assert sale_pool("cre", "2010-11-01", closing)[1] == "Commercial real estate · delinquent (30-89 days)"
    assert sale_pool("residential_1_4", "2011-01-01", closing) == (0, "Residential 1-4 family · performing")
    assert sale_pool("construction", None, closing) == (None, "Construction · performance unknown")


def test_schedule_tolerances_match_the_router():
    assert same("note_rate_pct", "8.25", 8.2504)
    assert not same("note_rate_pct", "8.25", 8.26)
    assert same("current_upb", "380589.55", 380590.0)
    assert not same("maturity_date", "2011-07-04", "2011-04-07")


def test_formatting():
    assert fmt("original_principal", "1051000.0") == "1,051,000.00"
    assert fmt("note_rate_pct", "8.25") == "8.25%"
    assert fmt("maturity_date", "2011-07-04T00:00:00") == "2011-07-04"
    assert fmt("current_upb", None) == ""
