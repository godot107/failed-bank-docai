"""Prescriptive review: leakage-free P(wrong), the active-learning update, and the exact knapsack (no Azure)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from prescribe import knapsack, p_wrong, rates  # noqa: E402

# (bank, item_id, reason, wrong, label_source)
LABELS = [("A", 1, "rule_failure", 1, "evaluation"), ("A", 2, "rule_failure", 0, "evaluation"),
          ("A", 3, "rule_failure", 0, "evaluation"), ("A", 4, "mismatch", 0, "evaluation"),
          ("B", 5, "rule_failure", 1, "evaluation"), ("B", 6, "rule_failure", 1, "evaluation"),
          ("B", 7, "rule_failure;mismatch", 1, "evaluation")]


def test_own_bank_evaluation_labels_never_score_it():
    t = rates(LABELS, exclude_bank="B")                 # B's answers are the key for B's open items
    assert t["rule_failure"][1:] == (1, 3)              # only bank A: 1 wrong of 3
    assert abs(t["rule_failure"][0] - (1 + 1) / (3 + 2)) < 1e-9   # Beta(1,1) smoothing


def test_reviewer_decisions_at_the_same_bank_update_the_estimate():
    before = p_wrong("rule_failure", rates(LABELS, "A"))[0]
    decided = LABELS + [("A", 8, "rule_failure", 1, "reviewer"), ("A", 9, "rule_failure", 1, "reviewer")]
    after = p_wrong("rule_failure", rates(decided, "A"))[0]
    assert after > before                               # two corrections at bank A raise A's remaining items


def test_combined_reason_falls_back_to_its_parts():
    p, basis = p_wrong("missing;mismatch", rates(LABELS, "B"))
    assert basis.endswith("(few labels)") and 0 < p < 1


def test_knapsack_is_exact_not_greedy():
    loans = [{"minutes": 6, "upb": 30.0}, {"minutes": 5, "upb": 26.0}, {"minutes": 5, "upb": 26.0}]
    chosen = knapsack(loans, 10)                        # greedy by value/minute takes the 6-minute loan (30)
    assert sum(ln["upb"] for ln in chosen) == 52.0      # the exact answer takes the two 5-minute loans
