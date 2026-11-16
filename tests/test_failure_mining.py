"""Failure mining: association-rule measures, quartile items and the permutation baseline (no Azure, no network)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from failure_mining import items_for, permutation_threshold, quantile, rules  # noqa: E402


def test_support_confidence_lift():
    baskets = [{"a"}, {"a"}, {"a", "b"}, {"b"}, set(), set(), set(), set(), set(), set()]
    target = [True, True, False, False, False, False, False, False, False, False]   # base rate 20%
    r = {tuple(x["antecedent"]): x for x in rules(baskets, target, min_support=0.1)}
    a = r[("a",)]
    assert a["count"] == 3 and abs(a["support"] - 0.3) < 1e-9
    assert abs(a["confidence"] - 2 / 3) < 1e-9 and abs(a["lift"] - (2 / 3) / 0.2) < 1e-9


def test_low_equity_is_the_risk_item():
    rows = [{k: None for k in ("construction_share", "cre_share", "residential_share", "multifamily_share", "farm_share",
                               "ci_share", "consumer_share", "brokered_share", "noncurrent_ratio", "loan_to_asset")}
            | {"equity_ratio": v} for v in (0.01, 0.05, 0.08, 0.10, 0.12)]
    baskets, cut = items_for(rows)
    assert "equity_ratio" in baskets[0] and "equity_ratio" not in baskets[-1]   # bottom quartile flagged


def test_shuffled_labels_set_the_bar():
    baskets = [{"a"} if i % 2 else set() for i in range(40)]
    target = [i % 2 == 1 for i in range(40)]            # "a" predicts perfectly: lift 2
    chance = permutation_threshold(baskets, target, runs=50)
    assert 1.0 < chance < 2.0                             # chance alone can't reach the real lift


def test_quantile_interpolates():
    assert quantile([1, 2, 3, 4], 0.5) == 2.5
