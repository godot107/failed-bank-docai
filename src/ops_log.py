"""Record pipeline runs for the operations report (ops.run_log + ops.stage_metric in Azure SQL).

Always appends to samples/synthetic/ops/runs.jsonl too, so runs are recorded even when SQL is paused or
unreachable; tools/load_ops.py (later) can backfill SQL from that file.
"""
from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from common import ROOT

LOG = ROOT / "samples" / "synthetic" / "ops" / "runs.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def run(stage: str, model_id: str | None = None, bank_id: str | None = None, to_sql: bool = True):
    """with run("extract", model_id="prebuilt-layout") as r: ...; r["pages"] += n; r["metrics"]["di_errors"] += 1"""
    rec = {"run_id": str(uuid.uuid4()), "stage": stage, "model_id": model_id, "bank_id": bank_id,
           "started_at": _now(), "status": "running", "pages": 0, "error": None,
           "metrics": {"items_in": 0, "items_out": 0, "di_errors": 0, "throttled": 0}}
    t0 = time.time()
    try:
        yield rec
        rec["status"] = "ok"
    except BaseException as e:
        rec["status"], rec["error"] = "failed", repr(e)[:2000]
        raise
    finally:
        rec["ended_at"] = _now()
        rec["metrics"]["latency_ms"] = round((time.time() - t0) * 1000)
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
        if to_sql:
            try:
                _to_sql(rec)
            except Exception as e:  # monitoring must never break the pipeline
                print(f"  (ops log to SQL skipped: {e.__class__.__name__})")


def _to_sql(rec: dict) -> None:
    from common import connect_sql
    conn = connect_sql(retries=2, wait=5)
    cur = conn.cursor()
    cur.execute("INSERT INTO ops.run_log (run_id, stage, bank_id, started_at, ended_at, status, error, pages, model_id) "
                "VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)",
                (rec["run_id"], rec["stage"], rec["started_at"], rec["ended_at"], rec["status"], rec["error"],
                 rec["pages"], rec["model_id"]))
    for k, v in rec["metrics"].items():
        cur.execute("INSERT INTO ops.stage_metric (run_id, metric, value) VALUES (?, ?, ?)", (rec["run_id"], k, float(v)))
    conn.commit()
