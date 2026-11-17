"""Measure the candidate loan-level rules (src/rules_v2.py) against the current rules (route.rule_fail), offline.

Two tests, both against the planted answer key; nothing in Azure, SQL or the app changes:

  1. Real misreads: every wrong value from the four extractors (rules baseline, neural v1, v2, v3). Which does each
     rule set catch? And false alarms: correct values of the deployed model (v3) each rule set sends to review.
  2. Planted misreads: realistic reading errors injected one at a time into correct v3 values (transposed / dropped
     / extra digits, OCR digit confusions, lost decimal point, wrong year digit, day-month swap). Catch rate by
     error type and field.

Caveat printed with the results: the synthetic loans are generated with the textbook amortization formula, so on
this data the amortization, term and balance rules never see a real loan's quirks (escrow in the payment, ARMs,
modifications, odd first periods). Catch rates carry over; false-alarm rates are optimistic.

Usage: .venv/bin/python tools/measure_rules_v2.py [--write docs/RULES_V2.md] [--seed 7]
"""
from __future__ import annotations

import argparse
import collections
import csv
import random
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import route  # noqa: E402
from rules_v2 import DATES, loan_problems_v2  # noqa: E402

EVAL = ROOT / "samples" / "synthetic" / "eval"
CLOSING = date(2010, 12, 17)          # every modeled bank closed this day (src/synth/stories.py CLOSING_DATE)
METHODS = ("rules", "fbd-neural-v1", "fbd-neural-v2", "fbd-neural-v3")


def load(method: str) -> list[dict]:
    rows = list(csv.DictReader(open(EVAL / f"{method}.csv")))
    for r in rows:
        r["confidence"] = float(r["confidence"]) if r["confidence"] not in ("", "None") else 0.0
    return rows


def index(rows):
    by_doc, by_loan = collections.defaultdict(dict), collections.defaultdict(dict)
    for r in rows:
        if r["outcome"] != "missing" and r["value"] not in ("", "None"):
            by_doc[(r["loan_id"], r["doc_type"])][r["field"]] = r["value"]
            by_loan[r["loan_id"]][r["field"]] = r["value"]
    return by_doc, by_loan


def flags(r, by_doc, by_loan, override=None):
    """(old flagged?, {rule: reason} from the new rules) for value r; override = {field: value} for planted errors."""
    if override:
        r = {**r, "value": override[r["field"]], "raw": str(override[r["field"]])}
        by_doc = {**by_doc, (r["loan_id"], r["doc_type"]): {**by_doc.get((r["loan_id"], r["doc_type"]), {}), **override}}
        by_loan = {**by_loan, r["loan_id"]: {**by_loan.get(r["loan_id"], {}), **override}}
    try:
        old = route.rule_fail(r, by_doc)
    except (ValueError, TypeError):
        old = True                      # unparseable → the current pipeline sends it to review
    new = loan_problems_v2(by_loan.get(r["loan_id"], {}), CLOSING)
    why = new.get(r["field"])
    return old, ({why.split(":")[0]: why} if why else {})


# ---------------------------------------------------------------- planted misreads
OCR = {"0": "8", "8": "0", "1": "7", "7": "1", "5": "6", "6": "5", "3": "8", "2": "7"}


def mutations(field: str, value: str, rng: random.Random) -> list[tuple[str, str]]:
    out = []
    if field in DATES:
        y, m, d = value[:4], value[5:7], value[8:10]
        out.append(("wrong year digit", f"{y[:3]}{(int(y[3]) + rng.choice([1, 2, -1])) % 10}-{m}-{d}"))
        if int(d) <= 12 and d != m:
            out.append(("day/month swap", f"{y}-{d}-{m}"))
        out.append(("wrong month", f"{y}-{(int(m) % 12) + 1:02d}-{d}"))
        return out
    s = f"{float(value):.3f}".rstrip("0").rstrip(".") if field == "note_rate_pct" else f"{float(value):.2f}"
    ip, _, fp = s.partition(".")
    digits = [i for i, c in enumerate(s) if c.isdigit()]
    if len(ip) >= 2:
        i = rng.randrange(len(ip) - 1)
        if ip[i] != ip[i + 1]:
            out.append(("transposed digits", ip[:i] + ip[i + 1] + ip[i] + ip[i + 2:] + (("." + fp) if fp else "")))
    cand = [i for i in digits if s[i] in OCR]
    if cand:
        i = rng.choice(cand)
        out.append(("OCR digit confusion", s[:i] + OCR[s[i]] + s[i + 1:]))
    if len(ip) >= 3:
        out.append(("dropped digit", ip[:-2] + ip[-1] + (("." + fp) if fp else "")))
        out.append(("extra digit", ip + ip[-1] + (("." + fp) if fp else "")))
    if fp:
        out.append(("decimal point lost", ip + fp))
    return [(k, v) for k, v in out if v != s and _ok(field, v)]


def _ok(field, v):
    try:
        (date.fromisoformat(v) if field in DATES else float(v))
        return True
    except ValueError:
        return False


def pct(a, b):
    return f"{a / b:.0%}" if b else "–"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    L = ["# Candidate loan-level rules: offline measurement", "",
         "Generated by `tools/measure_rules_v2.py` against the planted answer key. Rules: `src/rules_v2.py` "
         "(amortization, term, dates against the bank's closing, balance against the note's schedule). **Not wired "
         "into routing, SQL or the review app.** \"Current\" = the rules in production routing (`route.rule_fail`).",
         ""]

    # 1. real misreads
    L += ["## 1. Real misreads from the four extractors", "",
          "| Extractor | Wrong values | Caught by current rules | Caught by new rules | Caught by either | New catches the current rules missed |",
          "|---|---:|---:|---:|---:|---:|"]
    detail = []
    for m in METHODS:
        rows = [r for r in load(m) if not r["split"].startswith("train")]
        by_doc, by_loan = index(rows)
        wrong = [r for r in rows if r["outcome"] == "wrong"]
        o = n = e = add = 0
        for r in wrong:
            old, new = flags(r, by_doc, by_loan)
            o += old; n += bool(new); e += old or bool(new); add += bool(new) and not old
            detail.append((m, r["loan_id"], r["field"], r["truth"], r["value"], "yes" if old else "no",
                           ", ".join(new) or "no"))
        L.append(f"| {m} | {len(wrong)} | {o} | {n} | {e} | {add} |")
    L += ["", "Only held-out banks (validation, test, holdouts); training banks excluded. The rules baseline reads nothing "
          "on unseen banks (every value missing), so it has no wrong values to catch there.", "",
          "<details><summary>Each wrong value</summary>", "",
          "| Extractor | Loan | Field | True | Read | Current | New rule |", "|---|---|---|---|---|---|---|"]
    L += [f"| {' | '.join(map(str, d))} |" for d in detail] + ["", "</details>", ""]

    # false alarms on the deployed model
    rows = load("fbd-neural-v3")
    by_doc, by_loan = index(rows)
    correct = [r for r in rows if r["outcome"] == "correct"]
    fa_old = fa_new_only = fa_clean = 0
    bad_loans = {r["loan_id"] for r in rows if r["outcome"] != "correct"}
    by_rule = collections.Counter()
    for r in correct:
        old, new = flags(r, by_doc, by_loan)
        fa_old += old
        fa_new_only += bool(new) and not old
        fa_clean += bool(new) and not old and r["loan_id"] not in bad_loans
        for k in new:
            by_rule[k] += 1
    L += ["## False alarms on correct values (neural v3, all banks)", "",
          f"{len(correct):,} correct values. Current rules send **{fa_old}** ({pct(fa_old, len(correct))}) to review; "
          f"the new rules add **{fa_new_only}** ({pct(fa_new_only, len(correct))}). Of those, **{fa_clean}** are on a "
          f"loan with no misread; the rest are the other values on a note page that does have one (the rule sends the "
          f"page's values together, and the reviewer fixes the wrong one).", ""]
    if by_rule:
        L += ["| New rule | Correct values flagged |", "|---|---:|"] + [f"| {k} | {v} |" for k, v in by_rule.most_common()]
    L.append("")

    # 2. planted misreads
    rng = random.Random(a.seed)
    tally = collections.defaultdict(lambda: [0, 0, 0, 0])      # n, old, new, either
    by_field = collections.defaultdict(lambda: [0, 0, 0, 0])
    caught_by = collections.Counter()
    for r in correct:
        for kind, bad in mutations(r["field"], r["value"], rng):
            old, new = flags(r, by_doc, by_loan, {r["field"]: bad})
            for t in (tally[kind], by_field[r["field"]], tally["ALL"]):
                t[0] += 1; t[1] += old; t[2] += bool(new); t[3] += old or bool(new)
            for k in new:
                caught_by[k] += 1
    L += ["## 2. Planted misreads (one error at a time in a correct v3 value)", "",
          "| Error type | Planted | Current rules | New rules | Either |", "|---|---:|---:|---:|---:|"]
    for k in sorted(tally, key=lambda k: (k == "ALL", k)):
        n, o, nw, e = tally[k]
        label = f"**all**" if k == "ALL" else k
        L.append(f"| {label} | {n:,} | {pct(o, n)} | {pct(nw, n)} | {pct(e, n)} |")
    L += ["", "| Field | Planted | Current rules | New rules | Either |", "|---|---:|---:|---:|---:|"]
    for k in sorted(by_field):
        n, o, nw, e = by_field[k]
        L.append(f"| {k} | {n:,} | {pct(o, n)} | {pct(nw, n)} | {pct(e, n)} |")
    L += ["", "Which new rule caught them: " + ", ".join(f"{k} {v:,}" for k, v in caught_by.most_common()) + ".", "",
          "## Caveats", "",
          "- The synthetic loans are generated with the textbook amortization formula and whole-year terms, so the "
          "amortization, term and balance rules fit this data exactly. Real loans carry escrow in the payment, ARMs, "
          "modifications, odd first periods and curtailments: expect more false alarms, and tune the tolerances on a "
          "sample of real loans before turning a rule on.",
          "- Planted misreads test the rules alone, without the confidence score; in routing, a value goes to review "
          "if any check fires.",
          "- Real misreads are few (see the table): the planted test is what gives the catch rates."]
    text = "\n".join(L) + "\n"
    if a.write:
        Path(a.write).write_text(text)
        print(f"wrote {a.write}")
    print(text)


if __name__ == "__main__":
    main()
