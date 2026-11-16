import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from normalize import normalize, same  # noqa: E402


def test_money_formats_from_every_style():
    for s in ["$459,000.00", "USD 1,494,000.00", "1,494,000.00", "$1,494,000"]:
        assert normalize("original_principal", s) in (459000.0, 1494000.0)


def test_percent_formats():
    assert normalize("note_rate_pct", "9.125%") == 9.125
    assert normalize("note_rate_pct", "9.125") == 9.125
    assert normalize("note_rate_pct", "9.125 %") == 9.125


def test_date_formats_including_unseen_test_styles():
    for s in ["01/13/2004", "January 13, 2004", "13-Jan-2004", "2004-01-13", "Jan 13 2004"]:
        assert normalize("note_date", s) == "2004-01-13", s


def test_same_tolerates_whole_dollar_printing():
    assert same("current_upb", 459000.0, 459000.49)
    assert not same("current_upb", 459001.0, 459000.0)


def test_bank_date_order_and_ambiguity():
    from normalize import ambiguous_date, date_order
    assert date_order(["25.03.2008", "06.04.2008"]) == "dmy"
    assert date_order(["03/25/2008", "06/04/2008"]) == "mdy"
    assert date_order(["06.04.2008", "07.05.2009"]) is None          # never proven: both readings possible
    assert ambiguous_date("06.04.2008", None)
    assert not ambiguous_date("06.04.2008", "dmy")
    assert not ambiguous_date("25.03.2008", None)
    assert not ambiguous_date("April 6, 2008", None)
    assert normalize("note_date", "06.04.2008", "dmy") == "2008-04-06"
