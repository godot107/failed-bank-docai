"""Turnaround and correctness KPIs: the AI-assisted process vs a person keying the same values by hand.

Three questions, all answered from SQL so Power BI and the apps read the same numbers:

  1. How long does a person spend?   touch time: seconds a reviewer spends on one review item (the review app stamps
                                      opened_at and seconds_on_item on every decision), and seconds a person spends
                                      keying a document by hand (review.manual_entry, from the /manual page).
  2. How long until the data is usable?  cycle time per loan: file received -> last review decision applied.
  3. How often is it wrong?          field error rate against the planted answer key (eval.field_truth), for the
                                      model alone, for the model after review (silent errors), and for manual keying.

Objects (idempotent; safe to rerun after review_projection.py, which rebuilds review.item):
  review.item            + opened_at, seconds_on_item (written by the review app; kept in review.item_history)
  eval.field_truth       answer key per review document and field (synthetic labels only)
  review.manual_entry    one row per field a person keyed by hand on the /manual page, with the seconds for the document
  dash.v_touch           decided review items with their touch time
  dash.v_cycle           per loan: received, ready (no open items), hours between
  dash.v_manual          manual entries scored against the answer key
  dash.v_ai_vs_manual    one row per process: fields, error rate, human seconds per field, straight-through rate

Usage: .venv/bin/python src/turnaround.py --reviewer <work UPN>   (grants for the review app; the /manual page's
       identity comes from ASK_APP_NAME in .env)
       .venv/bin/python src/turnaround.py --report                 (print the comparison)
"""
from __future__ import annotations

import argparse
import json
import os
import re

from common import ROOT, connect_sql, load_env
from normalize import FIELD_TYPES, FIELDS_BY_DOC

SYN = ROOT / "samples" / "synthetic"
SHORT = {"promissory_note": "PN", "servicing_history": "SH", "appraisal_1004": "AP"}
DOC_OF = {v: k for k, v in SHORT.items()}
TOLERANCE = 0.005   # a keyed or extracted number counts as right within half a cent / 0.005 points; dates are exact

DDL = [
    "IF COL_LENGTH('review.item', 'opened_at') IS NULL ALTER TABLE review.item ADD opened_at DATETIME2 NULL",
    "IF COL_LENGTH('review.item', 'seconds_on_item') IS NULL ALTER TABLE review.item ADD seconds_on_item INT NULL",
    "IF SCHEMA_ID('eval') IS NULL EXEC('CREATE SCHEMA eval')",
    """IF OBJECT_ID('eval.field_truth') IS NULL CREATE TABLE eval.field_truth (
        doc_id NVARCHAR(16) NOT NULL, field NVARCHAR(64) NOT NULL, true_value FLOAT NULL, printed BIT NOT NULL,
        CONSTRAINT pk_field_truth PRIMARY KEY (doc_id, field))""",
    """IF OBJECT_ID('review.manual_entry') IS NULL CREATE TABLE review.manual_entry (
        entry_id INT IDENTITY PRIMARY KEY, session_id NVARCHAR(36) NOT NULL, doc_id NVARCHAR(16) NOT NULL,
        field NVARCHAR(64) NOT NULL, entered_value FLOAT NULL, seconds_on_doc INT NOT NULL,
        entered_by NVARCHAR(64) NULL, entered_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME())""",
]

VIEWS = [
    # touch time per decided item; seconds over 30 minutes are a reviewer who walked away, not work
    """CREATE OR ALTER VIEW dash.v_touch AS
       SELECT i.item_id, i.doc_id, i.field, i.flag_reasons AS reason, i.status AS decision, i.opened_at,
              i.reviewed_at, i.seconds_on_item,
              CASE WHEN i.seconds_on_item BETWEEN 0 AND 1800 THEN i.seconds_on_item END AS seconds_counted
       FROM review.item i WHERE i.status <> 'open' AND i.seconds_on_item IS NOT NULL""",
    # cycle time: a loan is ready when none of its review items are open
    """CREATE OR ALTER VIEW dash.v_cycle AS
       SELECT l.loan_id, l.loan_number, b.name AS bank, f.received_at,
              COUNT(DISTINCT r.item_id) AS review_items,
              COUNT(DISTINCT CASE WHEN r.status <> 'decided' THEN r.item_id END) AS open_items,
              CASE WHEN COUNT(DISTINCT CASE WHEN r.status <> 'decided' THEN r.item_id END) = 0
                   THEN COALESCE(MAX(d.decided_at), f.received_at) END AS ready_at,
              CASE WHEN COUNT(DISTINCT CASE WHEN r.status <> 'decided' THEN r.item_id END) = 0
                   THEN DATEDIFF(minute, f.received_at, COALESCE(MAX(d.decided_at), f.received_at)) / 60.0 END AS cycle_hours
       FROM desk.loan l JOIN desk.bank b ON b.bank_id = l.bank_id
       CROSS APPLY (SELECT MIN(lf.received_at) AS received_at FROM desk.loan_file lf
                    JOIN desk.document dd ON dd.file_id = lf.file_id
                    JOIN desk.extraction x ON x.doc_id = dd.doc_id AND x.loan_id = l.loan_id) f
       LEFT JOIN desk.review_item r ON r.loan_id = l.loan_id
       LEFT JOIN desk.review_decision d ON d.item_id = r.item_id
       GROUP BY l.loan_id, l.loan_number, b.name, f.received_at""",
    f"""CREATE OR ALTER VIEW dash.v_manual AS
       SELECT m.entry_id, m.session_id, m.doc_id, m.field, m.entered_value, t.true_value, t.printed, m.seconds_on_doc,
              m.entered_by, m.entered_at,
              CASE WHEN m.entered_value IS NULL AND t.true_value IS NULL THEN 1
                   WHEN m.entered_value IS NULL OR t.true_value IS NULL THEN 0
                   WHEN ABS(m.entered_value - t.true_value) <= {TOLERANCE} THEN 1 ELSE 0 END AS is_correct
       FROM review.manual_entry m JOIN eval.field_truth t ON t.doc_id = m.doc_id AND t.field = m.field""",
    # one row per process. AI human time = average review touch time x the share of fields sent to review
    # (auto-accepted fields take no human time).
    """CREATE OR ALTER VIEW dash.v_ai_vs_manual AS
       SELECT 'AI + review' AS process, 1 AS sort_order,
              COUNT(*) AS fields,
              CAST(AVG(CASE WHEN o.outcome = 'correct' THEN 0.0 ELSE 1.0 END) AS FLOAT) AS model_error_rate,
              CAST(AVG(CAST(o.silent_error AS FLOAT)) AS FLOAT) AS final_error_rate,
              CAST(AVG(CASE WHEN o.routed = 'auto' THEN 1.0 ELSE 0.0 END) AS FLOAT) AS straight_through_rate,
              (SELECT AVG(CAST(seconds_counted AS FLOAT)) FROM dash.v_touch)
                * AVG(CASE WHEN o.routed = 'review' THEN 1.0 ELSE 0.0 END) AS human_seconds_per_field,
              (SELECT COUNT(seconds_counted) FROM dash.v_touch) AS timed_samples
       FROM eval.field_outcome o
       UNION ALL
       SELECT 'Keyed by hand', 2, COUNT(*),
              NULL,
              CAST(1.0 - AVG(CAST(is_correct AS FLOAT)) AS FLOAT),
              0.0,
              SUM(CAST(seconds_on_doc AS FLOAT) / n.fields_in_session) / NULLIF(COUNT(*), 0),
              COUNT(DISTINCT session_id)
       FROM dash.v_manual v
       CROSS APPLY (SELECT COUNT(*) AS fields_in_session FROM review.manual_entry x
                    WHERE x.session_id = v.session_id) n""",
]


def as_number(field: str, v) -> float | None:
    """Same encoding as the review app: numbers stay numbers, ISO dates become YYYYMMDD numbers."""
    if v in (None, ""):
        return None
    if FIELD_TYPES.get(field) == "date":
        s = str(v).replace("-", "")[:8]
        return float(s) if s.isdigit() and len(s) == 8 else None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def truth_rows(doc_ids: list[str]) -> list[tuple]:
    """(doc_id, field, true_value, printed) for every field the document type is responsible for."""
    rows = []
    for doc_id in doc_ids:
        m = re.fullmatch(r"(.+)-(PN|SH|AP)", doc_id)
        if not m:
            continue
        loan_key, doc_type = m.group(1), DOC_OF[m.group(2)]
        lab_path = SYN / "labels" / f"{loan_key}.json"
        if not lab_path.exists():
            continue
        lab = json.loads(lab_path.read_text())
        printed = next((set(d["printed"]) for d in lab["documents"] if d["doc_type"] == doc_type), set())
        for field in FIELDS_BY_DOC[doc_type]:
            on_page = field in printed
            # a value that isn't printed on the page can't be keyed: the right answer is "blank"
            rows.append((doc_id, field, as_number(field, lab["fields"].get(field)) if on_page else None, on_page))
    return rows


def setup(cur, reviewer: str | None) -> None:
    for s in DDL:
        cur.execute(s)
    cur.execute("SELECT doc_id FROM review.document")
    docs = [r[0] for r in cur.fetchall()]
    cur.execute("DELETE FROM eval.field_truth")
    rows = truth_rows(docs)
    if rows:
        cur.executemany("INSERT INTO eval.field_truth (doc_id, field, true_value, printed) VALUES (?, ?, ?, ?)", rows)
    for v in VIEWS:
        cur.execute(v)
    if reviewer:
        if not re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", reviewer):
            raise SystemExit(f"--reviewer must be a user principal name: {reviewer!r}")
        q = reviewer.replace("]", "]]")
        cur.execute(f"GRANT UPDATE ON review.item (opened_at, seconds_on_item) TO [{q}]")
    web = os.environ.get("ASK_APP_NAME")
    if web:   # the /manual page: show the page image and the field list, record what was keyed; nothing else
        q = web.replace("]", "]]")
        cur.execute(f"GRANT SELECT ON review.document TO [{q}]")
        cur.execute(f"GRANT SELECT ON eval.field_truth TO [{q}]")
        cur.execute(f"GRANT SELECT, INSERT ON review.manual_entry TO [{q}]")
        cur.execute(f"GRANT SELECT ON dash.v_ai_vs_manual TO [{q}]")
    print(f"timing columns ready; answer key: {len(rows)} fields on {len(docs)} review documents")


def report(cur) -> None:
    cur.execute("SELECT process, fields, model_error_rate, final_error_rate, straight_through_rate, "
                "human_seconds_per_field, timed_samples FROM dash.v_ai_vs_manual ORDER BY sort_order")
    print(f"{'process':<15}{'fields':>8}{'model err':>11}{'final err':>11}{'no touch':>10}{'human s/field':>15}{'timed':>7}")
    for p, n, me, fe, st, hs, ts in cur.fetchall():
        pct = lambda x: "-" if x is None else f"{x:.1%}"  # noqa: E731
        print(f"{p:<15}{n:>8}{pct(me):>11}{pct(fe):>11}{pct(st):>10}{'-' if hs is None else f'{hs:.1f}':>15}{ts:>7}")
    cur.execute("SELECT COUNT(*), AVG(cycle_hours) FROM dash.v_cycle WHERE ready_at IS NOT NULL")
    n, h = cur.fetchone()
    cur.execute("SELECT COUNT(*) FROM dash.v_cycle WHERE ready_at IS NULL")
    waiting = cur.fetchone()[0]
    print(f"cycle time: {n} loans ready (avg {h or 0:.1f} h from received to ready), {waiting} waiting on review")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reviewer", help="work-account UPN that uses the review app (column grants)")
    ap.add_argument("--report", action="store_true", help="print the comparison only")
    a = ap.parse_args()
    load_env()
    conn = connect_sql()
    cur = conn.cursor()
    if not a.report:
        setup(cur, a.reviewer)
        conn.commit()
    report(cur)


if __name__ == "__main__":
    main()
