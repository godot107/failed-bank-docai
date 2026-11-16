import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from schemas import PATerms, TextFlagReply, loan_problems  # noqa: E402


def test_clean_loan_has_no_problems():
    assert loan_problems({"note_date": "2006-07-04", "maturity_date": "2011-07-04", "original_principal": "1051000",
                          "monthly_payment": "8452.13", "note_rate_pct": "8.25"}) == {}


def test_hard_range_error_reported_and_other_rules_still_run():
    p = loan_problems({"note_rate_pct": "62.5", "original_principal": "21436.46", "monthly_payment": "1051000"})
    assert "note_rate_pct" in p
    assert "original_principal" in p and "monthly_payment" in p      # swap still caught


def test_maturity_before_note_date():
    assert "maturity_date" in loan_problems({"note_date": "2008-04-06", "maturity_date": "2007-01-01"})


def test_reference_rules_ltv_and_balance():
    p = loan_problems({"appraised_value": "1811", "current_upb": "2000000", "reference_principal": "1500000"})
    assert set(p) == {"appraised_value", "current_upb"}


def _q(value, quote="text", page=1):
    return {"value": value, "quote": quote, "page": page}


def _terms(**over):
    base = {k: _q(None, None, None) for k in PATerms.model_fields}
    base.update(over)
    return PATerms.model_validate(base)


def test_unquoted_value_is_dropped():
    t = _terms(deposit_premium_pct=_q(1.0, quote=""))
    assert t.deposit_premium_pct.value is None


def test_loss_share_pct_needs_a_loss_share_quote():
    wrong = _terms(sf_loss_share_pct=_q(50.0, "fifty per cent (50%) of collections on Fully Charged-Off Assets"))
    right = _terms(sf_loss_share_pct=_q(80.0, "the Single Family Shared-Loss Agreement, which is eighty percent (80%)"))
    assert wrong.sf_loss_share_pct.value is None
    assert right.sf_loss_share_pct.value == 80.0


def test_extra_keys_rejected():
    with pytest.raises(ValidationError):
        PATerms.model_validate({**{k: _q(None, None, None) for k in PATerms.model_fields}, "note": "x"})


def test_text_flags_reject_unknown_flag_names():
    assert TextFlagReply.model_validate({"flags": [{"segment": 2, "flags": ["bankruptcy"]}]}).flags[0].segment == 2
    with pytest.raises(ValidationError):
        TextFlagReply.model_validate({"flags": [{"segment": 1, "flags": ["looks risky"]}]})
