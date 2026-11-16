import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "synth"))

import stories as st  # noqa: E402
import text as tx  # noqa: E402


def test_test_styles_never_in_training():
    train = {b["style"] for b in st.BANKS if b["split"] == "train"}
    for b in st.BANKS:
        if b["split"] in ("val", "test", "holdout", "holdout2", "holdout3"):
            assert b["style"] not in train, b


def test_loans_are_internally_consistent():
    for bank in st.BANKS:
        for loan in st.make_bank_loans(bank, 20, seed=3):
            assert loan.current_upb <= loan.original_principal + 0.01
            assert loan.maturity_date > loan.note_date
            assert 0 < loan.note_rate_pct < 25
            if loan.loan_type in st.SECURED:
                assert loan.appraised_value and loan.property_address
            if "income_mismatch" in loan.flags:
                assert loan.stated_income > loan.verified_income


def test_generation_is_deterministic():
    b = st.BANKS[0]
    a1 = [st.to_json(x) for x in st.make_bank_loans(b, 5, seed=11)]
    a2 = [st.to_json(x) for x in st.make_bank_loans(b, 5, seed=11)]
    assert a1 == a2


def test_every_style_has_text_for_every_flag():
    styles = {b.get("text_style", b["style"]) for b in st.BANKS}   # the style each bank's notes are written in
    for table in (tx.FLAG_TEXT, tx.MEMO_FLAG, tx.DECOY_TEXT):
        for flag, by_style in table.items():
            assert styles <= set(by_style), flag
