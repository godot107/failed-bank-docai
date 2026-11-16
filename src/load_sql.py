"""Load pipeline results into Azure SQL (desk.*, eval.*, ops.*, ref.*) for Power BI and Power Apps.

The "receivership desk" works the banks the model has NOT trained on: validation (B4), test (B5, B6) and the
holdout (B7). Their loan files go to the lake's raw container (evidence: hashed, never overwritten), and their
extracted values, routing decisions and review queue go to SQL.

  desk.bank / loan / loan_file / document / extraction / loan_field / review_item   (the working record)
  eval.field_outcome    answer-key outcomes (synthetic data only)
  ops.run_log / stage_metric / cost_daily   backfilled from samples/synthetic/ops/runs.jsonl
  ref.failure_year      FDIC failures per year (public)

Re-runnable: desk/eval are reloaded (loan_field history is cleared too); ops rows are added if missing.
Usage: .venv/bin/python src/load_sql.py [--model fbd-neural-v1] [--no-upload]
"""
from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import json
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "synth"))
import stories as st  # noqa: E402
from common import ROOT, connect_sql  # noqa: E402
from normalize import FIELDS_BY_DOC  # noqa: E402

SYN = ROOT / "samples" / "synthetic"
DESK_SPLITS = ("val", "test", "holdout", "holdout2")
PRIORITY = {"mismatch": 1, "rule_failure": 2, "missing": 3, "low_confidence": 3}
PRICE_PER_1K = {"prebuilt-layout": 10.0, "fbd-neural-v1": 30.0, "fbd-neural-v2": 30.0, "fbd-neural-v3": 30.0, "fbd-classifier-v1": 3.0}


def reset(cur, reason: str) -> None:
    """Demo reset: clear the working record so it can be reloaded. Never touches the audit trail: desk.audit_event
    is append-only, and loan_field rows are deleted WITH system versioning on, so every value stays in
    desk.loan_field_history (REVIEW.md D1). The reset itself is an audit event."""
    for t in ("desk.review_decision", "desk.review_item", "desk.text_flag", "desk.loan_field",
              "desk.extraction", "desk.document", "desk.loan_file", "desk.loan", "desk.bank", "eval.field_outcome"):
        cur.execute(f"DELETE FROM {t}")
    cur.execute("INSERT INTO desk.audit_event (actor, action, entity, entity_id, before_json, after_json) "
                "VALUES (SUSER_SNAME(), 'demo_reset', 'desk', '*', NULL, ?)", (json.dumps({"reason": reason}),))


def guard(cur, allow: bool) -> None:
    """Refuse to wipe a desk that holds reviewer decisions unless --reset-demo is given."""
    cur.execute("SELECT COUNT(*) FROM desk.review_decision")
    n = cur.fetchone()[0]
    if n and not allow:
        raise SystemExit(f"desk.review_decision holds {n} reviewer decision(s). Reloading would discard the working "
                         "record (the audit trail is kept). Re-run with --reset-demo if that's intended.")


def upload_raw(bank_id: str, loan_id: str) -> tuple[str, str, int]:
    pdf = (SYN / "pdf" / bank_id / f"{loan_id}.pdf").read_bytes()
    sha = hashlib.sha256(pdf).hexdigest()
    path = f"{bank_id}/{loan_id}.pdf"
    return path, sha, len(pdf)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="fbd-neural-v3")   # promoted 2026-11-11 (docs/RESULTS.md)
    ap.add_argument("--no-upload", action="store_true")
    ap.add_argument("--reset-demo", action="store_true", help="allow wiping a desk that holds reviewer decisions")
    a = ap.parse_args()

    conn = connect_sql()
    cur = conn.cursor()
    guard(cur, a.reset_demo)
    reset(cur, f"reload with {a.model}")
    conn.commit()

    from azure.storage.blob import ContentSettings
    from classifier import blob_container
    fs = None if a.no_upload else blob_container("raw")     # blob API on the lake (the DFS append path 404s here)
    index = [r for r in csv.DictReader(open(SYN / "index.csv")) if r["split"] in DESK_SPLITS]
    route = {(r["loan_id"], r["field"]): r for r in csv.DictReader(open(SYN / "eval" / f"route_{a.model}.csv"))}
    run_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rng = random.Random(7)

    bank_ids, loan_ids = {}, {}
    open_by_bank = collections.Counter()
    for b in st.BANKS:
        if b["split"] not in DESK_SPLITS:
            continue
        cur.execute("INSERT INTO desk.bank (fdic_cert, name, closing_date, acquirer, layout_family, split, status) "
                    "OUTPUT INSERTED.bank_id VALUES (?, ?, ?, NULL, ?, ?, 'in_review')",
                    (b["model_cert"], b["name"], st.CLOSING_DATE, b["style"], b["split"]))
        bank_ids[b["bank_id"]] = cur.fetchone()[0]

    n_items = 0
    for r in index:
        lab = json.loads((SYN / "labels" / f"{r['loan_id']}.json").read_text())
        bid = bank_ids[r["bank_id"]]
        cur.execute("INSERT INTO desk.loan (bank_id, loan_number) OUTPUT INSERTED.loan_id VALUES (?, ?)",
                    (bid, lab["fields"]["loan_number"]))
        lid = cur.fetchone()[0]
        loan_ids[r["loan_id"]] = lid
        path, sha, _ = upload_raw(r["bank_id"], r["loan_id"])
        if fs:
            blob = fs.get_blob_client(path)
            if not blob.exists():       # evidence is write-once
                blob.upload_blob((SYN / "pdf" / r["bank_id"] / f"{r['loan_id']}.pdf").read_bytes(),
                                 content_settings=ContentSettings(content_type="application/pdf"))
        cur.execute("INSERT INTO desk.loan_file (bank_id, blob_path, sha256, pages) OUTPUT INSERTED.file_id VALUES (?, ?, ?, ?)",
                    (bid, f"raw/{path}", sha, int(r["pages"])))
        file_id = cur.fetchone()[0]
        doc_ids = {}
        for d in lab["documents"]:
            cur.execute("INSERT INTO desk.document (file_id, doc_type, page_from, page_to, classifier_confidence, source) "
                        "OUTPUT INSERTED.doc_id VALUES (?, ?, ?, ?, NULL, 'model')",
                        (file_id, d["doc_type"], d["page_from"], d["page_to"]))
            doc_ids[d["doc_type"]] = cur.fetchone()[0]
        # extractions + current loan-tape values + review items
        for f in sorted((SYN / "docintel" / "extract" / a.model).glob(f"{r['loan_id']}__*.json")):
            doc_type = f.stem.split("__")[1]
            got = json.loads(f.read_text())
            for field, g in got.items():
                if field not in FIELDS_BY_DOC.get(doc_type, []):
                    continue        # the model sometimes returns a field from the wrong document type; ignore it
                rt = route.get((r["loan_id"], field))
                cur.execute("INSERT INTO desk.extraction (doc_id, loan_id, field, raw_value, normalized_value, confidence, page, "
                            "model_id, model_version, run_id) OUTPUT INSERTED.extraction_id VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                            (doc_ids[doc_type], lid, field, g.get("value"), rt["value"] if rt else None,
                             g.get("confidence"), a.model, a.model.rsplit("-", 1)[-1], run_id))
                xid = cur.fetchone()[0]
                if rt and rt["value"]:
                    cur.execute("INSERT INTO desk.loan_field (loan_id, field, value, source, extraction_id, changed_by, reason) "
                                "VALUES (?, ?, ?, 'model', ?, ?, ?)",
                                (lid, field, rt["value"], xid, a.model, "auto-accepted" if rt["route"] == "auto" else "pending review"))
        for (loan_key, field), rt in route.items():
            if loan_key != r["loan_id"]:
                continue
            if rt["route"] == "review":
                reason = rt["reasons"].split(";")[0]
                created = now - timedelta(hours=rng.uniform(1, 70))
                cur.execute("INSERT INTO desk.review_item (loan_id, field_or_flag, reason, priority, status, created_at, sla_due) "
                            "VALUES (?, ?, ?, ?, 'open', ?, ?)",
                            (lid, field, reason, PRIORITY.get(reason, 3), created, created + timedelta(hours=48)))
                n_items += 1
                open_by_bank[r["bank_id"]] += 1
            cur.execute("INSERT INTO eval.field_outcome (loan_id, bank_id, field, model_id, outcome, routed, reasons, silent_error, "
                        "confidence, scanned) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (lid, bid, field, a.model, rt["outcome"], rt["route"], rt["reasons"] or None,
                         rt["silent_error"] == "True", float(rt["confidence"]) if rt["confidence"] not in ("", "None") else None,
                         rt["scanned"] == "True"))
    for b, bid in bank_ids.items():
        cur.execute("UPDATE desk.bank SET status = ? WHERE bank_id = ?", ("in_review" if open_by_bank[b] else "approved", bid))
    conn.commit()
    print(f"desk: {len(bank_ids)} banks, {len(loan_ids)} loans, {n_items} open review items ({a.model})")

    # ops backfill
    cur.execute("SELECT CAST(run_id AS nvarchar(36)) FROM ops.run_log")
    have = {x[0].lower() for x in cur.fetchall()}
    added = 0
    costs = collections.defaultdict(lambda: [0.0, 0.0])
    for line in open(SYN / "ops" / "runs.jsonl"):
        rec = json.loads(line)
        day = rec["started_at"][:10]
        model = rec.get("model_id") or ""
        if rec["stage"].startswith("train"):
            hours = rec["metrics"].get("latency_ms", 0) / 3.6e6
            costs[(day, "docintel_training")][0] += hours
            costs[(day, "docintel_training")][1] += hours * 3.0
        elif rec["pages"]:
            costs[(day, "docintel")][0] += rec["pages"]
            costs[(day, "docintel")][1] += rec["pages"] * PRICE_PER_1K.get(model, 30.0) / 1000
        if rec["run_id"].lower() in have:
            continue
        cur.execute("INSERT INTO ops.run_log (run_id, stage, bank_id, started_at, ended_at, status, error, pages, model_id) "
                    "VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)",
                    (rec["run_id"], rec["stage"], rec["started_at"][:19], rec.get("ended_at", rec["started_at"])[:19],
                     rec["status"], rec.get("error"), rec["pages"], model))
        for k, v in rec["metrics"].items():
            cur.execute("INSERT INTO ops.stage_metric (run_id, metric, value) VALUES (?, ?, ?)", (rec["run_id"], k, float(v)))
        added += 1
    cur.execute("DELETE FROM ops.cost_daily")
    for (day, svc), (units, cost) in costs.items():
        cur.execute("INSERT INTO ops.cost_daily (day, service, units, cost_usd) VALUES (?, ?, ?, ?)", (day, svc, units, round(cost, 4)))
    conn.commit()
    print(f"ops: {added} runs added; cost rows {len(costs)} (≈ ${sum(c for _, c in costs.values()):.2f} logged DI spend)")

    # public reference: failures per year
    rows = json.loads((ROOT / "data" / "public" / "failures.json").read_text())
    agg = collections.defaultdict(lambda: [0, 0, 0.0])
    for x in rows:
        y = int(x["FAILYR"])
        agg[y][0 if x.get("RESTYPE") == "FAILURE" else 1] += 1
        agg[y][2] += float(x.get("COST") or 0) / 1000.0
    cur.execute("DELETE FROM ref.failure_year")
    for y, (f, asst, cost) in sorted(agg.items()):
        wave = "2008-2012" if 2008 <= y <= 2012 else ("2023" if y == 2023 else None)
        cur.execute("INSERT INTO ref.failure_year (year, failures, assistance, est_cost_musd, wave) VALUES (?, ?, ?, ?, ?)",
                    (y, f, asst, round(cost, 1), wave))
    conn.commit()
    print(f"ref: {len(agg)} years of FDIC failures")


if __name__ == "__main__":
    main()
