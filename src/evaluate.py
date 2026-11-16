"""Score extraction methods against the planted truth, by split (train / val / test banks).

For each method in samples/synthetic/docintel/extract/<method>/ and each field a document is responsible for:
  correct   the value matches the truth after normalization
  wrong     a value was returned but doesn't match (dangerous: silent unless flagged)
  missing   no value returned (safe: obviously needs a person)
Precision = correct / returned; recall = correct / expected.
Writes samples/synthetic/eval/<method>.csv (one row per field instance, with confidence) and prints a summary.
"""
from __future__ import annotations

import argparse
import collections
import csv
import json

from common import ROOT
from normalize import FIELD_TYPES, FIELDS_BY_DOC, ambiguous_date, date_order, normalize, same

SYN = ROOT / "samples" / "synthetic"


def bank_date_orders(method: str) -> dict[str, str | None]:
    """Each bank's date order, inferred from ALL dates the method read at that bank (no labels used)."""
    raws: dict[str, list[str]] = {}
    for f in (SYN / "docintel" / "extract" / method).glob("*.json"):
        bank = f.stem.split("-")[0]
        for field, g in json.loads(f.read_text()).items():
            if FIELD_TYPES.get(field) == "date" and g.get("value"):
                raws.setdefault(bank, []).append(g["value"])
    return {b: date_order(v) for b, v in raws.items()}


def rows_for(method: str):
    labels = {}
    orders = bank_date_orders(method)
    for f in sorted((SYN / "docintel" / "extract" / method).glob("*.json")):
        loan_id, doc_type = f.stem.split("__")
        if loan_id not in labels:
            labels[loan_id] = json.loads((SYN / "labels" / f"{loan_id}.json").read_text())
        lab = labels[loan_id]
        got = json.loads(f.read_text())
        doc = next(d for d in lab["documents"] if d["doc_type"] == doc_type)
        scanned = doc["scanned"]
        for field in FIELDS_BY_DOC[doc_type]:
            truth = lab["fields"].get(field)
            if truth is None or ("printed" in doc and field not in doc["printed"]):
                continue
            g = got.get(field) or {}
            order = orders.get(lab["bank_id"])
            val = normalize(field, g.get("value"), order)
            outcome = "missing" if val is None else ("correct" if same(field, val, truth) else "wrong")
            yield {"method": method, "loan_id": loan_id, "bank_id": lab["bank_id"], "split": lab["split"],
                   "style": lab["style"], "doc_type": doc_type, "field": field, "scanned": scanned,
                   "truth": truth, "raw": g.get("value"), "value": val, "confidence": g.get("confidence"),
                   "date_order": order, "ambiguous": ambiguous_date(g.get("value"), order),
                   "outcome": outcome}


def summarize(rows: list[dict], by=("split",)) -> list[dict]:
    agg = collections.defaultdict(collections.Counter)
    for r in rows:
        agg[tuple(r[k] for k in by)][r["outcome"]] += 1
    out = []
    for key, c in sorted(agg.items()):
        n, ret = sum(c.values()), c["correct"] + c["wrong"]
        out.append({**dict(zip(by, key)), "n": n, "recall": c["correct"] / n if n else 0,
                    "precision": c["correct"] / ret if ret else 0, "wrong": c["wrong"], "missing": c["missing"]})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("methods", nargs="+")
    ap.add_argument("--by", default="split")
    a = ap.parse_args()
    (SYN / "eval").mkdir(parents=True, exist_ok=True)
    for m in a.methods:
        rows = list(rows_for(m))
        with open(SYN / "eval" / f"{m}.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print(f"== {m}")
        for s in summarize(rows, tuple(a.by.split(","))):
            keys = [k for k in s if k not in ("n", "recall", "precision", "wrong", "missing")]
            print(f"  {' / '.join(str(s[k]) for k in keys):<32} n={s['n']:>4}  recall {s['recall']:.1%}  "
                  f"precision {s['precision']:.1%}  wrong {s['wrong']:>3}  missing {s['missing']:>3}")


if __name__ == "__main__":
    main()
