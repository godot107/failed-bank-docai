"""Review routing (U1): which extracted values go to a person, and what slips through.

A value is sent to review if ANY of these hold:
  - the model's confidence is below the cutoff
  - a validity rule fails (rate outside 0-25%, maturity not after the note date, balance above the original
    amount, loan-to-value outside 30-160%, value missing)
  - (with --reconcile) it disagrees with the data-room schedule
Everything else is auto-accepted. A *silent error* is an auto-accepted value that is wrong: the number that matters.

The cutoff is chosen on the VALIDATION bank (lowest cutoff with silent errors <= target), then applied once to the
TEST banks. Also reports the new-bank burn-in alternative: review everything from a bank until it's proven.

Usage: .venv/bin/python src/route.py fbd-neural-v1 [--target 0.02]
"""
from __future__ import annotations

import argparse
import collections
import csv
import functools
import json
import re

from common import ROOT
from normalize import FIELD_TYPES
from schemas import loan_problems

SYN = ROOT / "samples" / "synthetic"


def load(method: str) -> list[dict]:
    rows = list(csv.DictReader(open(SYN / "eval" / f"{method}.csv")))
    for r in rows:
        r["confidence"] = float(r["confidence"]) if r["confidence"] not in ("", "None") else 0.0
    return rows


@functools.lru_cache(maxsize=None)
def data_room(loan_id: str) -> dict:
    """The bank's own loan schedule (servicing system): an independent reference, not the answer key."""
    p = SYN / "labels" / f"{loan_id}.json"
    return json.loads(p.read_text())["data_room"] if p.exists() else {}      # no schedule → no reference value


PARTIAL_OK = re.compile(r"\d{1,2}\D+\d{1,2}\D+\d{2,4}|\d{4}-\d{2}-\d{2}|[A-Za-z]+\.? \d{1,2},? \d{4}|"
                        r"\d{1,2}[ -][A-Za-z]+[ -]\d{2,4}|[A-Za-z]+, [A-Za-z]+ \d{1,2}, \d{4}")


def rule_fail(r: dict, by_doc: dict) -> bool:
    """True if the value must go to a person. Format checks on the printed text stay here; the validity and
    plausibility rules live on the record (schemas.LoanTape)."""
    if r["outcome"] == "missing":
        return True
    if r.get("ambiguous") == "True":      # day/month could be swapped and the bank's order isn't proven yet
        return True
    f = r["field"]
    if FIELD_TYPES.get(f) == "date" and r.get("raw") and not PARTIAL_OK.search(r["raw"]):
        return True     # partial date (e.g. "Jan-2011"): no day, so it can't be trusted
    values = dict(by_doc.get((r["loan_id"], r["doc_type"]), {}))
    values[f] = r["value"]
    values["reference_principal"] = data_room(r["loan_id"]).get("original_principal")
    return f in loan_problems(values)


def reconcile_fail(r: dict) -> bool:
    if r["field"] not in ("original_principal", "note_rate_pct", "maturity_date", "current_upb") or r["outcome"] == "missing":
        return False
    lab = json.loads((SYN / "labels" / f"{r['loan_id']}.json").read_text())
    dr = lab["data_room"][r["field"]]
    v = r["value"]
    if r["field"] == "maturity_date":
        return str(v) != str(dr)
    return abs(float(v) - float(dr)) > (0.0005 if r["field"] == "note_rate_pct" else 1.0)


def reasons(r: dict, by_doc: dict, cutoff: float, reconcile: bool) -> list[str]:
    out = []
    if r["outcome"] == "missing":
        out.append("missing")
    elif r["confidence"] < cutoff:
        out.append("low_confidence")
    if r["outcome"] != "missing" and rule_fail(r, by_doc):
        out.append("rule_failure")
    if reconcile and reconcile_fail(r):
        out.append("mismatch")
    return out


def decisions(rows: list[dict], cutoff: float, reconcile: bool = True) -> list[dict]:
    """One row per extracted value: routed to review (with reasons) or auto-accepted."""
    by_doc = collections.defaultdict(dict)
    for r in rows:
        if r["outcome"] != "missing":
            by_doc[(r["loan_id"], r["doc_type"])][r["field"]] = r["value"]
    out = []
    for r in rows:
        why = reasons(r, by_doc, cutoff, reconcile)
        out.append({**r, "route": "review" if why else "auto", "reasons": ";".join(why),
                    "silent_error": (not why) and r["outcome"] == "wrong"})
    return out


def apply(rows: list[dict], cutoff: float, reconcile: bool) -> dict:
    by_doc = collections.defaultdict(dict)
    for r in rows:
        if r["outcome"] != "missing":
            by_doc[(r["loan_id"], r["doc_type"])][r["field"]] = r["value"]
    n = len(rows)
    review = silent = wrong = 0
    for r in rows:
        to_review = r["confidence"] < cutoff or rule_fail(r, by_doc) or (reconcile and reconcile_fail(r))
        review += to_review
        wrong += r["outcome"] == "wrong"
        silent += (not to_review) and r["outcome"] == "wrong"
    return {"n": n, "review_rate": review / n, "silent_rate": silent / n, "silent": silent, "wrong": wrong,
            "caught": (wrong - silent) / wrong if wrong else 1.0}


def choose_cutoff(rows: list[dict], target: float, reconcile: bool) -> float:
    for c in [i / 100 for i in range(0, 101)]:
        if apply(rows, c, reconcile)["silent_rate"] <= target:
            return c
    return 1.0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("method")
    ap.add_argument("--target", type=float, default=0.02)
    ap.add_argument("--write", action="store_true", help="write per-value decisions to eval/route_<method>.csv")
    a = ap.parse_args()
    rows = load(a.method)
    val = [r for r in rows if r["split"] == "val"]
    test = [r for r in rows if r["split"] == "test"]
    holdout = [r for r in rows if r["split"] == "holdout"]
    holdout2 = [r for r in rows if r["split"] == "holdout2"]
    holdout3 = [r for r in rows if r["split"] == "holdout3"]
    for reconcile in (False, True):
        c = choose_cutoff(val, a.target, reconcile)
        v, t = apply(val, c, reconcile), apply(test, c, reconcile)
        name = "confidence + rules + reconciliation" if reconcile else "confidence + rules"
        print(f"== {name}: cutoff {c:.2f} chosen on validation (target silent ≤ {a.target:.0%})")
        parts = [("val", v), ("test", t)] + ([("holdout", apply(holdout, c, reconcile))] if holdout else []) \
            + ([("holdout2", apply(holdout2, c, reconcile))] if holdout2 else []) \
            + ([("holdout3", apply(holdout3, c, reconcile))] if holdout3 else [])
        for label, m in parts:
            print(f"  {label:<5} n={m['n']:>4}  review {m['review_rate']:.1%}  silent errors {m['silent']} "
                  f"({m['silent_rate']:.1%})  wrong values caught {m['caught']:.0%}")
    if a.write:
        c = choose_cutoff(val, a.target, True)
        out = decisions(rows, c, True)
        with open(SYN / "eval" / f"route_{a.method}.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(out[0]))
            w.writeheader()
            w.writerows(out)
        print(f"wrote eval/route_{a.method}.csv ({sum(o['route'] == 'review' for o in out)} to review of {len(out)})")
    print("== new-bank burn-in: review 100% of a new bank's values until accuracy is proven → silent 0, review 100%")


if __name__ == "__main__":
    main()
