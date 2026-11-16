"""Loan view for the review app: the whole loan file on one screen, and a release decision per loan.

The review queue (review.item) works one uncertain value at a time. A receiver releases loans, not fields, and a
loan file is a packet: note, security instrument, appraisal, servicing history, comment log, letters. The checks
that matter span documents (principal on the note vs the bank's schedule, appraisal vs loan amount, dates across
documents), and the risk flags come from free text. This module builds that loan-level picture:

  desk.text_flag      risk flags from the comment logs, borrower letters and credit memos (gpt-4.1-mini, cached
                      per document in samples/synthetic/docintel/llm_flags/), each with its quote and whether the
                      quote was found verbatim in the page text
  review.loan         one row per loan: bank, loan number, type, documents, counts, and the release decision
                      (pending / released / hold) that the app writes. System-versioned: every decision is kept.
  review.loan_detail  the rows the loan screen shows, read-only:
                        field     every loan-tape field: value, source document, page, confidence, the bank's
                                  schedule value and whether they agree, and the review item if one exists
                        check     packet-wide rules over all fields together (loan-to-value, payment vs principal,
                                  date order), the same rules the router uses (schemas.loan_problems)
                        flag      a risk flag with its quote
                        document  the documents the classifier found in the file

Usage: .venv/bin/python src/loan_view.py --reviewer <work account UPN> [--llm-missing] [--reset-releases]
  --llm-missing     ask the model for flags on documents that have no cached reply (about 1 cent per bank)
  --reset-releases  demo reset: every loan back to 'pending' (release decisions otherwise survive a rebuild)
"""
from __future__ import annotations

import argparse
import json
import re

from common import ROOT, connect_sql
from normalize import FIELD_TYPES
from schemas import loan_problems
from text_flags import FLAGS, build_dataset, llm_predict, norm

SYN = ROOT / "samples" / "synthetic"
FLAG_MODEL = "gpt-4.1-mini"
LABELS = {
    "note_date": "Note date", "original_principal": "Original principal", "note_rate_pct": "Note rate (%)",
    "monthly_payment": "Monthly payment", "maturity_date": "Maturity date", "current_upb": "Current unpaid balance",
    "next_due_date": "Next due date", "appraised_value": "Appraised value",
}
FIELD_ORDER = list(LABELS)
FLAG_LABELS = {
    "bankruptcy": "Bankruptcy", "litigation": "Litigation", "hardship": "Hardship / modification request",
    "fraud_suspected": "Possible fraud", "property_damage": "Property damage or vacancy",
    "payoff_request": "Payoff request / refinance", "underwriting_exception": "Underwriting exception",
    "draw_without_inspection": "Draw paid without inspection",
}
DOC_LABELS = {
    "promissory_note": "Promissory note", "security_instrument": "Security instrument", "appraisal_1004": "Appraisal",
    "servicing_history": "Servicing history", "servicer_comments": "Servicer comment log",
    "borrower_letter": "Borrower letter", "credit_memo": "Credit memo", "settlement_statement": "Settlement statement",
    "loan_application_1003": "Loan application",
}
SCHEDULE_FIELDS = ("original_principal", "note_rate_pct", "maturity_date", "current_upb")

DDL = [
    "IF SCHEMA_ID('review') IS NULL EXEC('CREATE SCHEMA review')",
    """IF OBJECT_ID('review.loan') IS NULL CREATE TABLE review.loan (
        loan_id INT PRIMARY KEY, file_key NVARCHAR(16) NULL, loan_number NVARCHAR(40) NOT NULL,
        bank NVARCHAR(200) NOT NULL, loan_type NVARCHAR(40) NULL, documents NVARCHAR(600) NULL,
        fields_total INT NOT NULL, fields_auto INT NOT NULL, review_items INT NOT NULL,
        schedule_differs INT NOT NULL, rule_warnings INT NOT NULL, flags INT NOT NULL,
        release_status NVARCHAR(12) NOT NULL DEFAULT 'pending'
            CONSTRAINT ck_loan_release CHECK (release_status IN ('pending', 'released', 'hold')),
        release_note NVARCHAR(400) NULL, released_by NVARCHAR(256) NULL, released_at DATETIME2 NULL,
        valid_from DATETIME2 GENERATED ALWAYS AS ROW START HIDDEN NOT NULL CONSTRAINT df_loan_from DEFAULT SYSUTCDATETIME(),
        valid_to DATETIME2 GENERATED ALWAYS AS ROW END HIDDEN NOT NULL
            CONSTRAINT df_loan_to DEFAULT CONVERT(DATETIME2, '9999-12-31 23:59:59.9999999'),
        PERIOD FOR SYSTEM_TIME (valid_from, valid_to))
     WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = review.loan_history))""",
    "IF COL_LENGTH('review.loan', 'pool') IS NULL ALTER TABLE review.loan ADD pool NVARCHAR(80) NULL, days_past_due INT NULL",
    """IF OBJECT_ID('review.loan_detail') IS NULL CREATE TABLE review.loan_detail (
        detail_id INT IDENTITY PRIMARY KEY, loan_id INT NOT NULL, kind NVARCHAR(12) NOT NULL,
        sort_order INT NOT NULL, field NVARCHAR(60) NULL, label NVARCHAR(200) NOT NULL,
        value_text NVARCHAR(400) NULL, source_text NVARCHAR(400) NULL, schedule_text NVARCHAR(200) NULL,
        check_result NVARCHAR(16) NULL, detail_text NVARCHAR(1000) NULL, verified BIT NULL, item_id INT NULL)""",
]


def fmt(field: str, v) -> str:
    if v in (None, ""):
        return ""
    if FIELD_TYPES.get(field) == "date":
        return str(v)[:10]
    try:
        x = float(v)
    except ValueError:
        return str(v)
    return f"{x:.3f}".rstrip("0").rstrip(".") + "%" if field == "note_rate_pct" else f"{x:,.2f}"


def same(field: str, a, b) -> bool:
    """The router's reconciliation tolerances (route.reconcile_fail)."""
    if field == "maturity_date":
        return str(a)[:10] == str(b)[:10]
    try:
        return abs(float(a) - float(b)) <= (0.0005 if field == "note_rate_pct" else 1.0)
    except (TypeError, ValueError):
        return False


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def packet_checks(values: dict, problems: dict) -> list[tuple]:
    """One row per packet rule that applies to this loan: (label, field to review, ok|warn, what was compared).
    The pass/fail comes from schemas.loan_problems (the router's rules); this only adds the numbers behind it."""
    out = []
    principal, payment = _num(values.get("original_principal")), _num(values.get("monthly_payment"))
    ref, upb, appraised = _num(values.get("reference_principal")), _num(values.get("current_upb")), _num(values.get("appraised_value"))
    note, maturity = values.get("note_date"), values.get("maturity_date")
    if note and maturity:
        bad = "maturity_date" in problems
        out.append(("Date order (note → maturity)", "maturity_date", "warn" if bad else "ok",
                    problems.get("maturity_date") if bad else f"note {str(note)[:10]} → maturity {str(maturity)[:10]}"))
    if principal and payment:
        bad = "monthly_payment" in problems
        out.append(("Payment vs principal (note)", "monthly_payment", "warn" if bad else "ok",
                    problems.get("monthly_payment") if bad else
                    f"payment is {payment / principal:.1%} of principal (expected 0.1%-6%)"))
    if ref and upb is not None:
        bad = "current_upb" in problems
        out.append(("Balance vs original amount (servicing history vs schedule)", "current_upb", "warn" if bad else "ok",
                    problems.get("current_upb") if bad else f"balance {upb:,.0f} ≤ original {ref:,.0f}"))
    if ref and appraised:
        bad = "appraised_value" in problems
        out.append(("Loan-to-value (appraisal vs schedule)", "appraised_value", "warn" if bad else "ok",
                    problems.get("appraised_value") if bad else f"loan-to-value {ref / appraised:.0%} (expected 30%-160%)"))
    reported = {row[1] for row in out if row[2] == "warn"}
    if "monthly_payment" in reported:
        reported.add("original_principal")      # the payment rule flags both values with one message
    for f, why in problems.items():   # anything not already shown by a rule row (e.g. a value out of range)
        if f not in reported:
            out.append((f"{LABELS.get(f, f)} value", f, "warn", why))
    return out


POOL_TYPES = {"c_and_i": "Commercial & industrial", "cre": "Commercial real estate", "construction": "Construction",
              "residential_1_4": "Residential 1-4 family", "single_family": "Single-family residential",
              "consumer": "Consumer", "land": "Land"}


def sale_pool(loan_type: str | None, next_due, closing) -> tuple[int | None, str]:
    """Proposed loan-sale pool: loan type x performance status at the bank's closing date. Loans are sold in pools of
    similar loans (type, performance, collateral); performance comes from the next-due date read off the servicing
    history, so a misread date changes the pool, which is one more reason the value goes through review."""
    kind = POOL_TYPES.get((loan_type or "").lower(), (loan_type or "Other").replace("_", " ").capitalize())
    try:
        from datetime import date
        due = date.fromisoformat(str(next_due)[:10])
        dpd = max(0, (closing - due).days) if closing else None
    except (TypeError, ValueError):
        return None, f"{kind} · performance unknown"
    if dpd is None:
        return None, f"{kind} · performance unknown"
    status = "performing" if dpd < 30 else "delinquent (30-89 days)" if dpd < 90 else "non-performing (90+ days)"
    return dpd, f"{kind} · {status}"


def load_flags(cur, banks: set[str], ask_llm: bool) -> int:
    """Model flags → desk.text_flag, with the quote and whether it is verbatim on the page."""
    rows = [r for r in build_dataset() if r["bank_id"] in banks]
    if not ask_llm:   # only documents with a cached reply; never call the model by accident
        cached = {p.name for p in (SYN / "docintel" / "llm_flags").glob("*.json")}
        rows = [r for r in rows if f"{r['loan_id']}__{r['doc_type']}.json" in cached]
    preds = llm_predict(rows)
    cur.execute("""SELECT d.doc_id, d.doc_type, f.blob_path, l.loan_id FROM desk.document d
                   JOIN desk.loan_file f ON f.file_id = d.file_id
                   CROSS APPLY (SELECT TOP 1 x.loan_id FROM desk.extraction x JOIN desk.document dd ON dd.doc_id = x.doc_id
                                WHERE dd.file_id = d.file_id AND x.loan_id IS NOT NULL) l""")
    docs = {}
    for doc_id, doc_type, blob, loan_id in cur.fetchall():
        key = re.search(r"([A-Z]\d+-\d{4})\.pdf$", blob)
        if key:
            docs[(key.group(1), doc_type)] = (doc_id, loan_id)
    cur.execute("DELETE FROM desk.text_flag WHERE model_id = ?", (FLAG_MODEL,))
    n = 0
    for r, flags in zip(rows, preds):
        target = docs.get((r["loan_id"], r["doc_type"]))
        if not target or not flags:
            continue
        verified = norm(r["text"]) in r["page_text"]
        for flag in sorted(flags):
            cur.execute("""INSERT INTO desk.text_flag (doc_id, loan_id, flag, quote, quote_verified, model_id)
                           VALUES (?, ?, ?, ?, ?, ?)""", (target[0], target[1], flag, r["text"][:1000], verified, FLAG_MODEL))
            n += 1
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reviewer", required=True, help="the app's work account (user principal name)")
    ap.add_argument("--llm-missing", action="store_true", help="ask the model for flags on uncached documents")
    ap.add_argument("--reset-releases", action="store_true",
                    help="demo reset: start every loan at 'pending' (earlier decisions stay in review.loan_history)")
    a = ap.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", a.reviewer):
        raise SystemExit(f"--reviewer must be a user principal name (user@domain): {a.reviewer!r}")

    conn = connect_sql()
    cur = conn.cursor()
    for stmt in DDL:
        cur.execute(stmt)
    conn.commit()

    # loans in the desk, keyed by the file they came from (B4-0001 …)
    cur.execute("""SELECT l.loan_id, l.loan_number, b.name, MIN(f.blob_path), b.closing_date
                   FROM desk.loan l JOIN desk.bank b ON b.bank_id = l.bank_id
                   LEFT JOIN desk.extraction x ON x.loan_id = l.loan_id
                   LEFT JOIN desk.document d ON d.doc_id = x.doc_id
                   LEFT JOIN desk.loan_file f ON f.file_id = d.file_id
                   GROUP BY l.loan_id, l.loan_number, b.name, b.closing_date""")
    loans = []
    for loan_id, loan_number, bank, blob, closing in cur.fetchall():
        m = re.search(r"([A-Z]\d+-\d{4})\.pdf$", blob or "")
        loans.append({"loan_id": loan_id, "loan_number": loan_number, "bank": bank, "key": m.group(1) if m else None,
                      "closing": closing})
    banks = {l["key"].split("-")[0] for l in loans if l["key"]}

    n_flags = load_flags(cur, banks, a.llm_missing)
    conn.commit()

    # review items per loan field (the open queue lives in review.item; item_map links it back)
    cur.execute("SELECT loan_id, field, item_id FROM review.item_map")
    items = {(l, f): i for l, f, i in cur.fetchall()}

    # releases already decided survive a rebuild
    cur.execute("SELECT loan_id, release_status, release_note, released_by, released_at FROM review.loan")
    kept = {} if a.reset_releases else {r[0]: r[1:] for r in cur.fetchall()}
    cur.execute("DELETE FROM review.loan_detail")
    cur.execute("DELETE FROM review.loan")

    for loan in loans:
        lid = loan["loan_id"]
        lab_path = SYN / "labels" / f"{loan['key']}.json" if loan["key"] else None
        lab = json.loads(lab_path.read_text()) if lab_path and lab_path.exists() else {}
        schedule = lab.get("data_room", {})
        loan_type = _schedule_csv(loan["key"]).get(loan["loan_number"], {}).get("loan_type")

        cur.execute("""SELECT lf.field, lf.value, lf.source, d.doc_type, x.page, x.confidence, x.model_id
                       FROM desk.loan_field lf LEFT JOIN desk.extraction x ON x.extraction_id = lf.extraction_id
                       LEFT JOIN desk.document d ON d.doc_id = x.doc_id WHERE lf.loan_id = ?""", (lid,))
        fields = {r[0]: r for r in cur.fetchall()}
        rows = []

        # 1. every loan-tape field
        differs = 0
        for order, f in enumerate(FIELD_ORDER):
            if f not in fields and f not in schedule:
                continue
            _, value, source, doc_type, page, conf, model = fields.get(f, (f, None, None, None, None, None, None))
            parts = [DOC_LABELS.get(doc_type, doc_type or "not found")]
            if page:
                parts.append(f"p.{page}")
            if conf is not None:
                parts.append(f"conf {float(conf):.2f}")
            if source == "reviewer":
                parts.append("corrected by reviewer")
            sched = schedule.get(f) if f in SCHEDULE_FIELDS else None
            if sched in (None, ""):
                result = "info"
            elif value in (None, ""):
                result = "missing"
            else:
                result = "ok" if same(f, value, sched) else "differs"
            differs += result == "differs"
            rows.append(("field", order, f, LABELS[f], fmt(f, value), " · ".join(parts),
                         fmt(f, sched) if sched not in (None, "") else None, result, None, None, items.get((lid, f))))

        # 2. packet-wide rules over all fields together (cross-document: note vs appraisal vs servicing history)
        values = {f: fields[f][1] for f in fields}
        values["reference_principal"] = schedule.get("original_principal")
        problems = loan_problems(values)
        checks = packet_checks(values, problems)
        for order, (label, field, result, detail) in enumerate(checks):
            rows.append(("check", 100 + order, field, label, None, None, None, result, detail, None,
                         items.get((lid, field)) if field else None))

        # 3. risk flags with quotes
        cur.execute("""SELECT t.flag, t.quote, t.quote_verified, d.doc_type FROM desk.text_flag t
                       JOIN desk.document d ON d.doc_id = t.doc_id WHERE t.loan_id = ? ORDER BY t.flag_id""", (lid,))
        flags = cur.fetchall()
        for order, (flag, quote, verified, doc_type) in enumerate(flags):
            rows.append(("flag", 200 + order, flag, FLAG_LABELS.get(flag, flag), None, DOC_LABELS.get(doc_type, doc_type),
                         None, "flag", quote, bool(verified), None))

        # 4. the documents the classifier found
        cur.execute("""SELECT DISTINCT d.doc_type, d.page_from, d.page_to FROM desk.document d
                       JOIN desk.loan_file f ON f.file_id = d.file_id
                       WHERE f.blob_path LIKE ? ORDER BY d.page_from""", (f"%/{loan['key']}.pdf",))
        docs = cur.fetchall()
        for order, (doc_type, p1, p2) in enumerate(docs):
            pages = f"p.{p1}" if p1 == p2 else f"pp.{p1}-{p2}"
            rows.append(("document", 300 + order, doc_type, DOC_LABELS.get(doc_type, doc_type), pages, None, None,
                         None, None, None, None))

        n_items = sum(1 for r in rows if r[0] == "field" and r[10] is not None)
        dpd, pool = sale_pool(loan_type, values.get("next_due_date"), loan["closing"])
        status = kept.get(lid, ("pending", None, None, None))
        cur.execute("""INSERT INTO review.loan (loan_id, file_key, loan_number, bank, loan_type, documents, fields_total,
                         fields_auto, review_items, schedule_differs, rule_warnings, flags,
                         release_status, release_note, released_by, released_at, pool, days_past_due)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (lid, loan["key"], loan["loan_number"], loan["bank"],
                     POOL_TYPES.get((loan_type or "").lower(), (loan_type or "").replace("_", " ")) or None,
                     ", ".join(DOC_LABELS.get(d[0], d[0]) for d in docs)[:600],
                     sum(1 for r in rows if r[0] == "field"), sum(1 for r in rows if r[0] == "field" and r[10] is None),
                     n_items, differs, sum(1 for c in checks if c[2] == "warn"), len(flags), *status, pool, dpd))
        cur.executemany("""INSERT INTO review.loan_detail (loan_id, kind, sort_order, field, label, value_text, source_text,
                             schedule_text, check_result, detail_text, verified, item_id)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", [(lid, *r) for r in rows])

    # least privilege for the app's work account: read both, write only the release decision
    q = a.reviewer.replace("]", "]]")
    cur.execute("IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = ?) "
                f"EXEC('CREATE USER [{q}] FROM EXTERNAL PROVIDER')", (a.reviewer,))
    cur.execute(f"GRANT SELECT ON review.loan TO [{q}]")
    cur.execute(f"GRANT SELECT ON review.loan_detail TO [{q}]")
    cur.execute(f"GRANT UPDATE ON review.loan (release_status, release_note, released_by, released_at) TO [{q}]")
    conn.commit()
    cur.execute("SELECT kind, COUNT(*) FROM review.loan_detail GROUP BY kind")
    print(f"review.loan: {len(loans)} loans; desk.text_flag: {n_flags} flags; detail rows: {dict(cur.fetchall())}")


_SCHEDULES: dict[str, dict] = {}


def _schedule_csv(key: str | None) -> dict:
    """The bank's own loan schedule (data_room/<bank>.csv), by loan number: loan type and reference values."""
    if not key:
        return {}
    bank = key.split("-")[0]
    if bank not in _SCHEDULES:
        import csv
        path = SYN / "data_room" / f"{bank}.csv"
        _SCHEDULES[bank] = {r["loan_number"]: r for r in csv.DictReader(open(path))} if path.exists() else {}
    return _SCHEDULES[bank]


if __name__ == "__main__":
    main()
