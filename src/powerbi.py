"""Power BI as code, from Linux (no Desktop): Azure SQL dash views → push dataset, and a PBIR report → Fabric API.

    python src/powerbi.py publish      # (re)create dataset "Loan File Desk" and push rows from the dash views
    python src/powerbi.py report       # write powerbi/report/ (PBIR) and publish it bound to the dataset
    python src/powerbi.py report --dry-run

Pages (docs/DASHBOARD.md), each titled with the question it answers:
  1. Can we trust the loan tape?        accuracy, review load and silent errors per bank (U1)
  2. Is review keeping up?              open items, reasons, SLA (operations)
  3. Is the pipeline healthy, and what does it cost?   runs, failures, pages, spend (operations)
  4. Is it faster and more accurate than keying by hand?   error rate, human seconds per field, cycle time
  5. Context: U.S. bank failures 1934-2026            the 2008-2012 wave and 2023 (public FDIC data)

Push datasets don't refresh themselves: `publish` is the refresh (run after load_sql.py).
Auth: Power BI token from a separate az profile (same pattern as the sibling projects):
    AZURE_CONFIG_DIR=~/.azure-powerbi az login --allow-no-subscriptions --tenant <tenant-id>
Adapted from the sibling project's Power BI module (same PBIR helpers).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from common import ROOT

API = "https://api.powerbi.com/v1.0/myorg"
FABRIC = "https://api.fabric.microsoft.com/v1"
PBI_PROFILE = Path(os.environ.get("PBI_AZURE_CONFIG_DIR", Path.home() / ".azure-powerbi"))
OUT = ROOT / "powerbi" / "report"
BASE = ROOT / "powerbi" / "base"
SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition"
W, H = 1280, 720
DATASET = "Loan File Desk"
WORKSPACE = os.environ.get("PBI_WORKSPACE", "failed-bank-docai")

# dataset table name → SQL source
TABLES = {
    "quality": "SELECT * FROM dash.v_quality",
    "banks": "SELECT bank_id, name, split, layout_family, status, loans, field_values, open_items FROM dash.v_bank",
    "queue": """SELECT item_id, bank, loan_number, field, reason, priority, status, extracted, CAST(confidence AS float) AS confidence,
                       created_at, sla_due, age_hours, breached FROM dash.v_queue""",
    "runs": """SELECT CAST(run_id AS nvarchar(36)) AS run_id, day, stage, status, model_id, pages, seconds,
                      COALESCE(di_errors, 0) AS di_errors, COALESCE(items_out, 0) AS items_out, failed FROM dash.v_runs""",
    "cost": "SELECT day, service, units, cost_usd FROM dash.v_cost",
    "failures": "SELECT year, failures, assistance, est_cost_musd, COALESCE(wave, 'other years') AS wave FROM ref.failure_year",
    # turnaround and correctness vs keying by hand (src/turnaround.py)
    "compare": """SELECT process, sort_order, fields, model_error_rate, final_error_rate, straight_through_rate,
                         human_seconds_per_field, timed_samples FROM dash.v_ai_vs_manual""",
    "touch": "SELECT item_id, field, reason, decision, CAST(seconds_counted AS float) AS seconds FROM dash.v_touch",
    "cycle": """SELECT loan_number, bank, review_items, open_items, CAST(cycle_hours AS float) AS cycle_hours,
                       CASE WHEN review_items = 0 THEN 'No review needed' ELSE 'Needed review' END AS path,
                       CASE WHEN ready_at IS NULL THEN 'Waiting on review' ELSE 'Ready' END AS state FROM dash.v_cycle""",
}
# declared types for columns that can be empty (an all-blank column would otherwise be typed String)
COLUMN_TYPES = {"model_error_rate": "Double", "final_error_rate": "Double", "straight_through_rate": "Double",
                "human_seconds_per_field": "Double", "seconds": "Double", "cycle_hours": "Double",
                "item_id": "Int64", "fields": "Int64", "timed_samples": "Int64", "sort_order": "Int64"}
MEASURES = {
    "quality": {
        "Values extracted": "COUNTROWS(quality)",
        "Recall": "DIVIDE(SUM(quality[is_correct]), COUNTROWS(quality))",
        "Precision": "DIVIDE(SUM(quality[is_correct]), SUM(quality[is_correct]) + SUM(quality[is_wrong]))",
        "Review load": "DIVIDE(SUM(quality[is_review]), COUNTROWS(quality))",
        "Silent errors": "SUM(quality[silent_error]) + 0",
        "Silent error rate": "DIVIDE(SUM(quality[silent_error]), COUNTROWS(quality))",
        "Wrong values caught": "DIVIDE(SUM(quality[is_wrong]) - SUM(quality[silent_error]), SUM(quality[is_wrong]))",
    },
    "queue": {
        "Open items": "CALCULATE(COUNTROWS(queue), queue[status] <> \"decided\") + 0",
        "Past SLA": "CALCULATE(SUM(queue[breached]), queue[status] <> \"decided\") + 0",
        "Oldest open (hours)": "CALCULATE(MAX(queue[age_hours]), queue[status] <> \"decided\")",
    },
    "runs": {
        "Runs": "COUNTROWS(runs)",
        "Failed runs": "SUM(runs[failed]) + 0",
        "Pages processed": "SUM(runs[pages])",
        "DI errors": "SUM(runs[di_errors]) + 0",
    },
    "cost": {
        "DI spend": "SUM(cost[cost_usd])",
        "Cost per 1K pages": "DIVIDE(CALCULATE(SUM(cost[cost_usd]), cost[service] = \"docintel\"), SUM(runs[pages])) * 1000",
    },
    "compare": {
        "AI error after review": "CALCULATE(MAX(compare[final_error_rate]), compare[process] = \"AI + review\")",
        "Manual error rate": "CALCULATE(MAX(compare[final_error_rate]), compare[process] = \"Keyed by hand\")",
        "No human touch": "CALCULATE(MAX(compare[straight_through_rate]), compare[process] = \"AI + review\")",
        "AI human sec per field": "CALCULATE(MAX(compare[human_seconds_per_field]), compare[process] = \"AI + review\")",
        "Manual sec per field": "CALCULATE(MAX(compare[human_seconds_per_field]), compare[process] = \"Keyed by hand\")",
    },
    "touch": {
        "Items timed": "COUNT(touch[seconds]) + 0",
        "Avg seconds per item": "AVERAGE(touch[seconds])",
    },
    "cycle": {
        "Loans ready": "CALCULATE(COUNTROWS(cycle), cycle[state] = \"Ready\") + 0",
        "Loans waiting": "CALCULATE(COUNTROWS(cycle), cycle[state] = \"Waiting on review\") + 0",
        "Avg cycle hours": "AVERAGE(cycle[cycle_hours])",
    },
    "failures": {
        "Failures 2008-2012": "CALCULATE(SUM(failures[failures]), failures[year] >= 2008, failures[year] <= 2012)",
        "Failures 1993-2007": "CALCULATE(SUM(failures[failures]), failures[year] >= 1993, failures[year] <= 2007)",
        "Failures 2023": "CALCULATE(SUM(failures[failures]), failures[year] = 2023)",
    },
}
FORMATS = {"Recall": "0.0%", "Precision": "0.0%", "Review load": "0.0%", "Silent error rate": "0.0%",
           "Wrong values caught": "0%", "AI error after review": "0.0%", "Manual error rate": "0.0%",
           "No human touch": "0.0%", "AI human sec per field": "0.0", "Manual sec per field": "0.0",
           "Avg seconds per item": "0", "Avg cycle hours": "0.0", "DI spend": "\\$#,0.00", "Cost per 1K pages": "\\$#,0.00"}
COLUMN_FORMATS = {"model_error_rate": "0.0%", "final_error_rate": "0.0%", "straight_through_rate": "0.0%",
                  "human_seconds_per_field": "0.0", "cycle_hours": "0.0", "cost_usd": "$#,0.00", "est_cost_musd": "#,0", "confidence": "0.00"}
COLUMN_FORMATS.update({c: "yyyy-mm-dd" for c in ("day", "created_at", "sla_due")})


# ------------------------------------------------------------------ auth + REST
def token(resource: str) -> str:
    if os.environ.get("WEBSITE_INSTANCE_ID"):   # running in Azure (the Function App): its managed identity
        from azure.identity import DefaultAzureCredential
        return DefaultAzureCredential().get_token(f"{resource.rstrip('/')}/.default").token
    env = dict(os.environ, AZURE_CONFIG_DIR=str(PBI_PROFILE))
    out = subprocess.run(["az", "account", "get-access-token", "--resource", resource, "--query", "accessToken", "-o", "tsv"],
                         capture_output=True, text=True, env=env)
    if out.returncode:
        raise SystemExit("No Power BI login. Run:\n  AZURE_CONFIG_DIR=~/.azure-powerbi az login --allow-no-subscriptions "
                         "--tenant <tenant-id>\n" + out.stderr[-300:])
    return out.stdout.strip()


def session(resource="https://analysis.windows.net/powerbi/api"):
    import requests
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {token(resource)}"
    return s


def pbi_type(s) -> str:
    import pandas as pd
    if pd.api.types.is_bool_dtype(s):
        return "Boolean"
    if pd.api.types.is_integer_dtype(s):
        return "Int64"
    if pd.api.types.is_float_dtype(s):
        return "Double"
    if pd.api.types.is_datetime64_any_dtype(s):
        return "DateTime"
    return "String"


def read_frames():
    import pandas as pd

    from common import connect_sql
    conn = connect_sql()
    frames = {}
    for t, q in TABLES.items():
        cur = conn.cursor()
        cur.execute(q)
        cols = [d[0] for d in cur.description]
        df = pd.DataFrame.from_records([tuple(r) for r in cur.fetchall()], columns=cols)
        for c in df.columns:
            vals = set(df[c].dropna().unique()) if df[c].dtype == object else None
            if vals is not None and vals and vals <= {True, False}:
                df[c] = df[c].astype("boolean")
            elif df[c].dtype == object and df[c].dropna().map(lambda v: hasattr(v, "as_integer_ratio")).all() and df[c].notna().any():
                df[c] = df[c].astype(float)   # SQL decimal → float
            elif df[c].dtype == object and df[c].dropna().map(lambda v: hasattr(v, "isoformat")).all() and df[c].notna().any():
                df[c] = pd.to_datetime(df[c])
        frames[t] = df
    return frames


def workspace_id(s) -> str:
    g = s.get(f"{API}/groups", params={"$filter": f"name eq '{WORKSPACE}'"}).json()["value"]
    return g[0]["id"] if g else s.post(f"{API}/groups?workspaceV2=True", json={"name": WORKSPACE}).json()["id"]


def publish(recreate: bool = False):
    s = session()
    gid = workspace_id(s)
    base = f"{API}/groups/{gid}"
    frames = read_frames()
    if recreate:   # push datasets can't gain tables: drop it (its report goes too; `report` rebuilds it from code)
        for d in s.get(f"{base}/datasets").json()["value"]:
            if d["name"] == DATASET:
                s.delete(f"{base}/datasets/{d['id']}").raise_for_status()
                print(f"deleted dataset {d['id']} (rebuilding with the new tables)")
    tables = [{"name": t, "columns": [{"name": c, "dataType": COLUMN_TYPES.get(c) or pbi_type(df[c]),
                                       **({"formatString": COLUMN_FORMATS[c]} if c in COLUMN_FORMATS else {})} for c in df.columns],
               "measures": [{"name": m, "expression": e, **({"formatString": FORMATS[m]} if m in FORMATS else {})}
                            for m, e in MEASURES.get(t, {}).items()]} for t, df in frames.items()]
    existing = [d for d in s.get(f"{base}/datasets").json()["value"] if d["name"] == DATASET]
    if existing:
        did = existing[0]["id"]
        for tdef in tables:
            r = s.put(f"{base}/datasets/{did}/tables/{tdef['name']}", json=tdef)
            if not r.ok:
                raise SystemExit(f"update {tdef['name']}: {r.status_code} {r.text[:300]} (new table? rerun with --recreate)")
    else:
        r = s.post(f"{base}/datasets", json={"name": DATASET, "defaultMode": "Push", "tables": tables})
        if not r.ok:
            raise SystemExit(f"create dataset failed: {r.status_code} {r.text[:500]}")
        did = r.json()["id"]
    for t, df in frames.items():
        rows = json.loads(df.to_json(orient="records", date_format="iso"))
        s.delete(f"{base}/datasets/{did}/tables/{t}/rows").raise_for_status()
        for i in range(0, len(rows), 5000):
            s.post(f"{base}/datasets/{did}/tables/{t}/rows", json={"rows": rows[i:i + 5000]}).raise_for_status()
        print(f"{t:16} {len(rows):6} rows")
    print(f"dataset: https://app.powerbi.com/groups/{gid}/datasets/{did}")
    return gid, did


# ------------------------------------------------------------------ PBIR helpers (same shapes as insurance-docai)
def vid(*parts):
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:20]


def col(t, c):
    return {"Column": {"Expression": {"SourceRef": {"Entity": t}}, "Property": c}}


def meas(t, m):
    return {"Measure": {"Expression": {"SourceRef": {"Entity": t}}, "Property": m}}


def agg(t, c, fn=0):   # 0 = Sum, 1 = Avg, 3 = Min, 4 = Max
    return {"Aggregation": {"Expression": col(t, c), "Function": fn}}


def proj(field, label=None):
    if "Measure" in field:
        t, p = field["Measure"]["Expression"]["SourceRef"]["Entity"], field["Measure"]["Property"]
        ref, native = f"{t}.{p}", p
    elif "Aggregation" in field:
        c = field["Aggregation"]["Expression"]["Column"]
        t, p = c["Expression"]["SourceRef"]["Entity"], c["Property"]
        fn = {0: "Sum", 1: "Avg", 3: "Min", 4: "Max"}[field["Aggregation"]["Function"]]
        ref, native = f"{fn}({t}.{p})", f"{fn} of {p}"
    else:
        t, p = field["Column"]["Expression"]["SourceRef"]["Entity"], field["Column"]["Property"]
        ref, native = f"{t}.{p}", p
    out = {"field": field, "queryRef": ref, "nativeQueryRef": native}
    if label:
        out["displayName"] = label
    return out


def lit(v):
    return {"expr": {"Literal": {"Value": v}}}


def where(t, c, values):
    return {"Version": 2, "From": [{"Name": "x", "Entity": t, "Type": 0}],
            "Where": [{"Condition": {"In": {
                "Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "x"}}, "Property": c}}],
                "Values": [[{"Literal": {"Value": v}}] for v in values]}}}]}


def filt(name, t, c, values):
    return {"name": name, "field": col(t, c), "type": "Categorical", "filter": where(t, c, values)}


def not_blank(name, t, c):
    cond = {"Not": {"Expression": {"In": {
        "Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "x"}}, "Property": c}}],
        "Values": [[{"Literal": {"Value": "null"}}]]}}}}
    return {"name": name, "field": col(t, c), "type": "Advanced",
            "filter": {"Version": 2, "From": [{"Name": "x", "Entity": t, "Type": 0}], "Where": [{"Condition": cond}]}}


NO_TOTALS = {"total": [{"properties": {"totals": {"expr": {"Literal": {"Value": "false"}}}}}]}


def table(page, key, x, y, w, h, buckets, **kw):
    return visual(page, key, "tableEx", x, y, w, h, buckets, objects=NO_TOTALS, **kw)


def visual(page, key, vtype, x, y, w, h, buckets=None, title=None, filters=None, objects=None, z=0, sort=None, top=None):
    v = {"visualType": vtype}
    if buckets:
        v["query"] = {"queryState": {b: {"projections": [proj(*f) if isinstance(f, tuple) else proj(f) for f in fs]}
                                     for b, fs in buckets.items()}}
        if sort:
            v["query"]["sortDefinition"] = {"sort": [{"field": sort[0], "direction": sort[1]}], "isDefaultSort": False}
    if objects:
        v["objects"] = objects
    if title:
        v["visualContainerObjects"] = {"title": [{"properties": {"show": lit("true"), "text": lit(f"'{title}'")}}]}
    v["drillFilterOtherVisuals"] = True
    out = {"$schema": f"{SCHEMA}/visualContainer/2.14.0/schema.json", "name": vid(page, key),
           "position": {"x": x, "y": y, "z": z, "width": w, "height": h, "tabOrder": z}, "visual": v}
    if filters:
        out["filterConfig"] = {"filters": filters}
    return key, out


def text(page, key, x, y, w, h, runs):
    paras = [{"textRuns": [{"value": t, **({"textStyle": s} if s else {})} for t, s in p]} for p in runs]
    return visual(page, key, "textbox", x, y, w, h, objects={"general": [{"properties": {"paragraphs": paras}}]})


def card(page, key, x, y, m, w=200, h=100):
    return visual(page, key, "card", x, y, w, h, {"Values": [m]}, objects={"categoryLabels": [{"properties": {"fontSize": lit("10D")}}],
                                                                       "labels": [{"properties": {"labelDisplayUnits": lit("1D")}}]})


def slicer(page, key, x, y, w, h, c, title):
    return visual(page, key, "slicer", x, y, w, h, {"Values": [c]}, title=title,
                  objects={"data": [{"properties": {"mode": lit("'Dropdown'")}}]})


H1 = {"fontWeight": "bold", "fontSize": "20pt"}
NOTE = {"fontSize": "10pt", "color": "#605E5C"}


def header(p, title, note):
    return text(p, "title", 20, 8, 1240, 64, [[(title, H1)], [(note, NOTE)]])


# ------------------------------------------------------------------ pages
SYNTH = ("Synthetic loan files (fictional banks); accuracy is measured against the planted answer key. "
         "Banks shown were never seen in training.")


def page_trust():
    p, q = "trust", "quality"
    return p, "Can we trust the loan tape?", [
        header(p, "Can we trust the loan tape?", "Auto-accepted only if confident, valid and matching the data-room schedule; the rest "
                                                 "goes to a person. Synthetic loan files, fictional banks the model never trained on."),
        card(p, "n", 20, 80, meas(q, "Values extracted"), w=240),
        card(p, "recall", 270, 80, meas(q, "Recall"), w=240),
        card(p, "precision", 520, 80, meas(q, "Precision"), w=240),
        card(p, "review", 770, 80, meas(q, "Review load"), w=240),
        card(p, "silent", 1020, 80, meas(q, "Silent errors"), w=240),
        visual(p, "bybank", "clusteredBarChart", 20, 195, 620, 250,
               {"Category": [(col(q, "bank"), "Bank")], "Y": [(meas(q, "Recall"), "Recall"), (meas(q, "Review load"), "Review load")]},
               title="Recall and review load by bank (new banks only)"),
        visual(p, "byfield", "clusteredBarChart", 650, 195, 610, 250,
               {"Category": [(col(q, "field"), "Field")], "Y": [(meas(q, "Recall"), "Recall")]},
               title="Which fields are hardest?", sort=(meas(q, "Recall"), "Ascending")),
        table(p, "detail", 20, 455, 1240, 250,
              {"Values": [(col(q, "bank"), "Bank"), (col(q, "split"), "Role"), (meas(q, "Values extracted"), "Values"),
                          (meas(q, "Recall"), "Recall"), (meas(q, "Precision"), "Precision"), (meas(q, "Review load"), "Review"),
                          (meas(q, "Silent errors"), "Silent errors"), (meas(q, "Wrong values caught"), "Wrong caught")]},
              title="Per bank: a rising silent-error count means the bank goes back to full review"),
    ]


def page_queue():
    p, w = "queue", "queue"
    return p, "Is review keeping up?", [
        header(p, "Is review keeping up?", "Open items by reason and age (48-hour SLA; past-SLA items are escalated). Item ages are "
                                           "simulated for the demo: the queue was loaded at once."),
        card(p, "open", 20, 80, meas(w, "Open items"), w=300),
        card(p, "sla", 330, 80, meas(w, "Past SLA"), w=300),
        card(p, "oldest", 640, 80, meas(w, "Oldest open (hours)"), w=300),
        slicer(p, "bank", 950, 80, 310, 100, col(w, "bank"), "Bank"),
        visual(p, "reason", "clusteredBarChart", 20, 195, 400, 250,
               {"Category": [(col(w, "reason"), "Reason")], "Y": [(meas(w, "Open items"), "Open items")]},
               title="Why items need a person"),
        visual(p, "bybank", "clusteredBarChart", 430, 195, 400, 250,
               {"Category": [(col(w, "bank"), "Bank")], "Y": [(meas(w, "Open items"), "Open items")]},
               title="Where the work is"),
        visual(p, "byfield", "clusteredBarChart", 840, 195, 420, 250,
               {"Category": [(col(w, "field"), "Field")], "Y": [(meas(w, "Open items"), "Open items")]},
               title="Which fields"),
        table(p, "items", 20, 455, 1240, 250,
              {"Values": [(col(w, "priority"), "Priority"), (col(w, "bank"), "Bank"), (col(w, "loan_number"), "Loan"),
                          (col(w, "field"), "Field"), (col(w, "reason"), "Reason"), (col(w, "extracted"), "Extracted"),
                          (agg(w, "confidence", 1), "Confidence"), (agg(w, "age_hours", 4), "Age (h)")]},
              title="Queue (highest priority first)", sort=(col(w, "priority"), "Ascending")),
    ]


def page_ops():
    p, r, c = "ops", "runs", "cost"
    return p, "Is the pipeline healthy?", [
        header(p, "Is the pipeline healthy, and what does it cost?", "Every pipeline run is logged (stage, pages, errors, latency). "
                                                                     "A failed run or Document Intelligence errors trigger an email alert."),
        card(p, "runs", 20, 80, meas(r, "Runs"), w=240),
        card(p, "failed", 270, 80, meas(r, "Failed runs"), w=240),
        card(p, "pages", 520, 80, meas(r, "Pages processed"), w=240),
        card(p, "spend", 770, 80, meas(c, "DI spend"), w=240),
        card(p, "per1k", 1020, 80, meas(c, "Cost per 1K pages"), w=240),
        visual(p, "pages_stage", "clusteredBarChart", 20, 195, 620, 250,
               {"Category": [(col(r, "stage"), "Stage")], "Y": [(agg(r, "pages"), "Pages")]}, title="Pages by stage"),
        visual(p, "spend_svc", "clusteredBarChart", 650, 195, 610, 250,
               {"Category": [(col(c, "service"), "Service")], "Y": [(agg(c, "cost_usd"), "Spend ($)")]}, title="Spend by service"),
        table(p, "runlog", 20, 455, 1240, 250,
              {"Values": [(col(r, "day"), "Day"), (col(r, "stage"), "Stage"), (col(r, "model_id"), "Model"), (col(r, "status"), "Status"),
                          (agg(r, "pages"), "Pages"), (agg(r, "seconds"), "Seconds"), (agg(r, "di_errors"), "DI errors")]},
              title="Run log", sort=(col(r, "day"), "Descending")),
    ]


def page_context():
    p, f = "context", "failures"
    return p, "Context: bank failures", [
        header(p, "Context: U.S. bank failures, 1934-2026", "The 2008-2012 wave (mortgage bubble) and 2023 (Silicon Valley Bank, "
                                                             "Signature, First Republic). Source: FDIC failures data (public)."),
        card(p, "wave", 20, 80, meas(f, "Failures 2008-2012"), w=300),
        card(p, "before", 330, 80, meas(f, "Failures 1993-2007"), w=300),
        card(p, "y2023", 640, 80, meas(f, "Failures 2023"), w=300),
        visual(p, "byyear", "clusteredColumnChart", 20, 195, 1240, 510,
               {"Category": [(col(f, "year"), "Year")], "Y": [(agg(f, "failures"), "Failures")], "Series": [(col(f, "wave"), "Wave")]},
               title="Bank failures per year", filters=[]),
    ]


def page_turnaround():
    p, k, t, c = "turnaround", "compare", "touch", "cycle"
    return p, "Faster than keying by hand?", [
        header(p, "Is it faster and more accurate than keying by hand?",
               "Error rate against the answer key; human time from the review app (seconds per decision) and from the /manual "
               "page, where a person keys the same documents by hand, timed. Manual figures stay blank until someone does it."),
        card(p, "ai_err", 20, 80, meas(k, "AI error after review"), w=240),
        card(p, "man_err", 270, 80, meas(k, "Manual error rate"), w=240),
        card(p, "stp", 520, 80, meas(k, "No human touch"), w=240),
        card(p, "ai_sec", 770, 80, meas(k, "AI human sec per field"), w=240),
        card(p, "man_sec", 1020, 80, meas(k, "Manual sec per field"), w=240),
        table(p, "cmp", 20, 195, 1240, 130,
              {"Values": [(col(k, "process"), "Process"), (agg(k, "fields"), "Fields"), (agg(k, "model_error_rate", 4), "Model error"),
                          (agg(k, "final_error_rate", 4), "Final error"), (agg(k, "straight_through_rate", 4), "No human touch"),
                          (agg(k, "human_seconds_per_field", 4), "Human sec / field"), (agg(k, "timed_samples"), "Timed samples")]},
              title="Side by side (AI human time = review seconds per item x share of fields sent to review)",
              sort=(agg(k, "sort_order", 3), "Ascending")),
        visual(p, "touch_reason", "clusteredBarChart", 20, 335, 400, 370,
               {"Category": [(col(t, "reason"), "Reason")], "Y": [(meas(t, "Avg seconds per item"), "Avg seconds")]},
               title="Review time per item, by why it was flagged"),
        visual(p, "touch_decision", "clusteredBarChart", 430, 335, 400, 370,
               {"Category": [(col(t, "decision"), "Decision")], "Y": [(meas(t, "Avg seconds per item"), "Avg seconds")]},
               title="Review time per item, by decision"),
        visual(p, "cycle_path", "clusteredBarChart", 840, 335, 420, 370,
               {"Category": [(col(c, "path"), "Path")], "Y": [(meas(c, "Avg cycle hours"), "Avg hours")]},
               title="Hours from file received to loan ready"),
    ]


PAGES = [page_trust, page_queue, page_ops, page_turnaround, page_context]


def write_definition(dataset_id: str, workspace: str):
    if OUT.exists():
        shutil.rmtree(OUT)
    shutil.copytree(BASE / "StaticResources", OUT / "StaticResources")
    (OUT / "definition").mkdir(parents=True)
    for f in ("report.json", "version.json"):
        shutil.copy(BASE / "definition" / f, OUT / "definition" / f)
    shutil.copy(BASE / ".platform", OUT / ".platform")
    (OUT / "definition.pbir").write_text(json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json",
        "version": "4.0",
        "datasetReference": {"byConnection": {"connectionString":
            f"Data Source=powerbi://api.powerbi.com/v1.0/myorg/{workspace};initial catalog=\"{DATASET}\";"
            f"integrated security=ClaimsToken;semanticmodelid={dataset_id}"}}}, indent=2))
    pdir = OUT / "definition" / "pages"
    order, n = [], 0
    for fn in PAGES:
        key, name, visuals = fn()
        pid = vid("page", key)
        order.append(pid)
        (pdir / pid / "visuals").mkdir(parents=True)
        (pdir / pid / "page.json").write_text(json.dumps({"$schema": f"{SCHEMA}/page/2.1.0/schema.json", "name": pid,
                                                          "displayName": name, "displayOption": "FitToPage",
                                                          "height": H, "width": W}, indent=2))
        for _, v in visuals:
            (pdir / pid / "visuals" / v["name"]).mkdir()
            (pdir / pid / "visuals" / v["name"] / "visual.json").write_text(json.dumps(v, indent=2))
            n += 1
    (pdir / "pages.json").write_text(json.dumps({"$schema": f"{SCHEMA}/pagesMetadata/1.1.0/schema.json",
                                                 "pageOrder": order, "activePageName": order[0]}, indent=2))
    return len(order), n


def wait(s, resp):
    if resp.status_code == 200:
        return resp.json() if resp.content else None
    if resp.status_code != 202:
        raise SystemExit(f"Fabric API {resp.status_code}: {resp.text[:500]}")
    op = resp.headers["x-ms-operation-id"]
    for _ in range(60):
        time.sleep(int(resp.headers.get("Retry-After", 5)))
        st = s.get(f"{FABRIC}/operations/{op}").json()
        if st["status"] == "Succeeded":
            r = s.get(f"{FABRIC}/operations/{op}/result")
            return r.json() if r.ok and r.content else None
        if st["status"] == "Failed":
            raise SystemExit(f"Fabric operation failed: {json.dumps(st)[:800]}")
    raise SystemExit("Fabric operation timed out")


def report(dry_run: bool):
    if dry_run:
        n_pages, n = write_definition("00000000-0000-0000-0000-000000000000", WORKSPACE)
        print(f"wrote {OUT.relative_to(ROOT)}: {n_pages} pages, {n} visuals (dry run, placeholder dataset id)")
        return
    s = session()
    gid = workspace_id(s)
    did = next(d["id"] for d in s.get(f"{API}/groups/{gid}/datasets").json()["value"] if d["name"] == DATASET)
    rep = next((r for r in s.get(f"{API}/groups/{gid}/reports").json()["value"] if r["name"] == DATASET), None)
    n_pages, n = write_definition(did, WORKSPACE)
    fab = session("https://api.fabric.microsoft.com")
    parts = [{"path": p.relative_to(OUT).as_posix(), "payloadType": "InlineBase64",
              "payload": base64.b64encode(p.read_bytes()).decode()} for p in sorted(OUT.rglob("*")) if p.is_file()]
    body = {"definition": {"parts": parts}}
    if rep:
        wait(fab, fab.post(f"{FABRIC}/workspaces/{gid}/reports/{rep['id']}/updateDefinition", json=body))
        rid = rep["id"]
    else:
        res = wait(fab, fab.post(f"{FABRIC}/workspaces/{gid}/reports", json={"displayName": DATASET, **body}))
        rid = res["id"] if res else "?"
    print(f"published {n_pages} pages / {n} visuals: https://app.powerbi.com/groups/{gid}/reports/{rid}")


def main():
    from common import load_env
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["publish", "report"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--recreate", action="store_true", help="publish: drop and rebuild the dataset (after adding a table)")
    a = ap.parse_args()
    publish(a.recreate) if a.cmd == "publish" else report(a.dry_run)


if __name__ == "__main__":
    main()
