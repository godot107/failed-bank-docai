"""Baseline: rules over the Layout output, written from the TRAINING banks' label wording only.

This is what a quick hand-built parser looks like: it knows "Principal Amount", "Original Loan Amount" and
"AMT FINANCED" because those appear in the training banks' documents. A new bank that prints "Sum Borrowed"
is exactly where it breaks, and that's the point of measuring it on held-out banks.

Output: samples/synthetic/docintel/extract/rules/<doc_key>.json  {field: {"value": raw, "confidence": None}}
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "synth"))
from common import ROOT  # noqa: E402
from normalize import FIELDS_BY_DOC  # noqa: E402

SYN = ROOT / "samples" / "synthetic"
OUT = SYN / "docintel" / "extract" / "rules"

# Label wording seen in the TRAINING styles (S1, S2) only. Validation (S3) and test (S4, S5) are unseen.
KNOWN = {
    "original_principal": ["principal amount", "original loan amount"],
    "note_rate_pct": ["interest rate", "note rate"],
    "maturity_date": ["maturity date", "final payment date"],
    "monthly_payment": ["monthly payment", "payment (p&i)"],
    "current_upb": ["unpaid principal balance", "current principal balance"],
    "next_due_date": ["next payment due", "due date"],
    "appraised_value": ["appraised value", "market value"],
    "note_date": ["date of note", "loan date"],
}


def kv_pairs(layout: dict) -> list[tuple[str, str]]:
    """(label, value) from two-column table rows, plus 'Label: value' inside lines."""
    pairs = []
    for t in layout.get("tables", []):
        rows: dict[int, dict[int, str]] = {}
        for c in t["cells"]:
            rows.setdefault(c["rowIndex"], {})[c["columnIndex"]] = c.get("content", "")
        for r in rows.values():
            if 0 in r and 1 in r:
                pairs.append((r[0], r[1]))
    for p in layout.get("pages", []):
        for ln in p.get("lines", []):
            for m in re.finditer(r"([A-Za-z][A-Za-z ()&/]{2,40}):\s*([^:]+?)(?=\s{2,}|\s+[A-Z][a-z]+ [A-Za-z.]+:|$)", ln["content"]):
                pairs.append((m.group(1), m.group(2)))
    return pairs


def extract(layout: dict, doc_type: str) -> dict:
    pairs = [(k.strip().lower().rstrip(":"), v.strip()) for k, v in kv_pairs(layout)]
    out = {}
    for field in FIELDS_BY_DOC.get(doc_type, []):
        for k, v in pairs:
            if k in KNOWN[field]:
                out[field] = {"value": v, "confidence": None}
                break
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in sorted((SYN / "docintel" / "layout").glob("*.json")):
        doc_type = f.stem.split("__")[1]
        if doc_type in FIELDS_BY_DOC:
            (OUT / f.name).write_text(json.dumps(extract(json.loads(f.read_text()), doc_type)))
            n += 1
    print(f"rules: {n} documents → {OUT}")


if __name__ == "__main__":
    main()
