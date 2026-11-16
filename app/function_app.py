"""Loan Desk Q&A: the web front end for the loan-desk-analyst agent (src/agent.py).

One Azure Functions app (Flex Consumption, Python 3.12):
    GET  /desk        the page (static/index.html); /desk?loan=231370616 pre-fills questions about that loan
                      (the Power Apps loan view links here)
    POST /api/ask     {"question": "...", "loan": "231370616"?} → answer, sources, quote checks, timing, cost
    GET  /api/health
Sign-in: App Service Authentication (Entra ID) in front of every route; unauthenticated requests never reach this code.
Identity to Azure: the app's managed identity (Foundry agent, Azure SQL read-only user, app storage). No keys.
Cost controls: answers cached until the data changes (a reviewer decision or a reload changes the key), and a
per-user daily question cap (ASK_DAILY_QUESTIONS) counted in Table Storage across instances.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

import azure.functions as func

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)   # Entra auth is enforced by the platform
_local = threading.local()
PAGE = (Path(__file__).parent / "static" / "index.html").read_text()
MAX_Q = 500
DAILY = int(os.environ.get("ASK_DAILY_QUESTIONS", "40"))
CACHE_ON = os.environ.get("ASK_CACHE", "1") == "1"
SALT = os.environ.get("ASK_LOG_SALT", "")
CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "frame-ancestors 'none'")


def pid(uid: str | None) -> str | None:
    """Pseudonymous user id for logs and counters (keyed hash of the Entra object id)."""
    return None if not uid else hashlib.sha256(f"{SALT}:{uid}".encode()).hexdigest()[:16]


def _conn():
    """One SQL connection per worker thread; reopened if the database paused and dropped it."""
    from common import connect_sql
    c = getattr(_local, "conn", None)
    if c is not None:
        try:
            cur = c.cursor()
            cur.execute("SELECT 1")
            cur.fetchall()
            cur.close()
        except Exception:  # noqa: BLE001
            c = None
    if c is None:
        c = _local.conn = connect_sql(retries=6, wait=15)
    return c


def _credential():
    c = getattr(_local, "cred", None)
    if c is None:
        from azure.identity import DefaultAzureCredential
        c = _local.cred = DefaultAzureCredential()
    return c


# ------------------------------------------------------------------ per-user daily cap (Table Storage)
def _table():
    t = getattr(_local, "table", None)
    if t is None:
        from azure.data.tables import TableClient
        t = _local.table = TableClient(f"https://{os.environ['APP_STORAGE_ACCOUNT']}.table.core.windows.net",
                                       "usage", credential=_credential())
    return t


def _count_question(uid: str | None) -> int:
    """Add one question to today's count for this user and return the new count (optimistic concurrency)."""
    from azure.core import MatchConditions
    pk, rk = time.strftime("%Y-%m-%d", time.gmtime()), pid(uid) or "anonymous"
    for _ in range(4):
        try:
            e = _table().get_entity(pk, rk)
            e["questions"] = int(e.get("questions", 0)) + 1
            _table().update_entity(e, etag=e.metadata["etag"], match_condition=MatchConditions.IfNotModified)
            return e["questions"]
        except Exception as ex:  # noqa: BLE001
            if "ResourceNotFound" in type(ex).__name__ + str(ex):
                try:
                    _table().create_entity({"PartitionKey": pk, "RowKey": rk, "questions": 1})
                    return 1
                except Exception:  # noqa: BLE001 - created meanwhile by another instance: retry the update
                    continue
    logging.warning("usage counter update failed")
    return 0


# ------------------------------------------------------------------ answer cache (key changes when the data does)
def _data_version() -> str | None:
    """Latest change to the review state (temporal tables) and the knowledge load: any decision invalidates."""
    try:
        cur = _conn().cursor()
        cur.execute("""SELECT CONVERT(varchar(27), (SELECT MAX(valid_from) FROM review.loan), 126) + '|' +
                              CONVERT(varchar(27), (SELECT MAX(valid_from) FROM review.item), 126) + '|' +
                              CAST((SELECT COUNT(*) FROM kb.passage) AS varchar(10))""")
        v = cur.fetchall()[0][0]
        cur.close()
        return hashlib.sha256(str(v).encode()).hexdigest()[:12]
    except Exception:  # noqa: BLE001
        return None


def _cache_key(q: str) -> str | None:
    v = _data_version()
    if not v:
        return None
    from agent import DATA_SPEC, INSTRUCTIONS, SEARCH_SPEC
    # a new agent version (instructions or tools) must not be served old answers
    agent_v = hashlib.sha256((INSTRUCTIONS + json.dumps([DATA_SPEC, SEARCH_SPEC])).encode()).hexdigest()[:8]
    norm = re.sub(r"[^a-z0-9 ]", "", " ".join(q.lower().split()))
    return f"{v}-{agent_v}/{hashlib.sha256(norm.encode()).hexdigest()[:32]}.json"


def _blob(name: str):
    from azure.storage.blob import BlobClient
    return BlobClient(f"https://{os.environ['APP_STORAGE_ACCOUNT']}.blob.core.windows.net", "answercache", name,
                      credential=_credential())


def _cache_get(key: str | None) -> dict | None:
    if not (CACHE_ON and key):
        return None
    try:
        return json.loads(_blob(key).download_blob().readall())
    except Exception:  # noqa: BLE001 - miss
        return None


def cacheable(r: dict) -> bool:
    """Only clean answers are reused: completed, no error, every quote found word-for-word in the evidence, nothing
    removed by the document shield, and figures not flagged as ungrounded."""
    g = r.get("guard") or {}
    return (r.get("status") == "completed" and not r.get("error") and bool(r.get("answer"))
            and all(q.get("verified") for q in r.get("quotes") or [])
            and all(f.get("verified") for f in r.get("figures") or [])
            and not g.get("docs_removed") and not g.get("shield_error")
            and not (g.get("groundedness") or {}).get("ungrounded"))


def _guard_events(r: dict, uid: str | None) -> None:
    """One structured log line per guardrail trigger (App Insights traces; no question or answer text)."""
    g = r.get("guard") or {}
    events = []
    if g.get("platform_block"):
        events.append("platform_block")
    if g.get("docs_removed"):
        events.append("injection_removed")
    if g.get("shield_error"):
        events.append("shield_error")
    if (g.get("groundedness") or {}).get("ungrounded"):
        events.append("ungrounded_figures")
    if any(not q.get("verified") for q in r.get("quotes") or []):
        events.append("unverified_quote")
    if any(not f.get("verified") for f in r.get("figures") or []):
        events.append("untraced_figure")
    for kind in events:
        logging.warning("GUARD_EVENT %s", json.dumps({"kind": kind, "user": pid(uid), "docs_removed": g.get("docs_removed", 0)}))


def _cache_put(key: str | None, body: dict) -> None:
    if CACHE_ON and key and cacheable(body):
        try:
            _blob(key).upload_blob(json.dumps(body, default=str), overwrite=True)
        except Exception:  # noqa: BLE001
            logging.warning("cache write failed")


# ------------------------------------------------------------------ routes
def _json(body: dict, status: int = 200) -> func.HttpResponse:
    return func.HttpResponse(json.dumps(body, default=str), status_code=status, mimetype="application/json",
                             headers={"Cache-Control": "no-store"})


@app.route(route="desk", methods=["GET"])
def desk(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse(PAGE, mimetype="text/html",
                             headers={"Cache-Control": "no-store", "Content-Security-Policy": CSP,
                                      "X-Content-Type-Options": "nosniff"})


@app.route(route="api/health", methods=["GET"])
def health(req: func.HttpRequest) -> func.HttpResponse:
    return _json({"ok": True})


@app.route(route="api/ask", methods=["POST"])
def ask(req: func.HttpRequest) -> func.HttpResponse:
    t0 = time.time()
    uid = req.headers.get("x-ms-client-principal-id")
    try:
        body = req.get_json()
    except ValueError:
        return _json({"error": "send JSON: {\"question\": \"...\"}"}, 400)
    q = " ".join(str(body.get("question") or "").split())
    loan = re.sub(r"\D", "", str(body.get("loan") or ""))[:12]
    if not q:
        return _json({"error": "empty question"}, 400)
    if len(q) > MAX_Q:
        return _json({"error": f"questions are limited to {MAX_Q} characters"}, 400)
    question = f"(Context: I'm looking at loan {loan} in the loan view.) {q}" if loan and loan not in q else q
    key = _cache_key(question)
    hit = _cache_get(key)
    if hit:
        hit.update({"cached": True, "seconds": round(time.time() - t0, 2)})
        logging.warning("ASK_EVENT %s", json.dumps({"user": pid(uid), "cached": True, "seconds": hit["seconds"]}))
        return _json(hit)
    n = _count_question(uid)
    if n > DAILY:
        return _json({"error": f"daily limit reached ({DAILY} new questions per person per day); cached answers still work"}, 429)
    from agent import ask as agent_ask
    try:
        r = agent_ask(question, _conn(), verbose=False)
    except Exception as e:  # noqa: BLE001
        logging.exception("ask failed")
        return _json({"error": f"the agent couldn't answer right now ({type(e).__name__}); try again in a minute"}, 502)
    r.update({"cached": False, "seconds": round(time.time() - t0, 2), "question": q})
    _guard_events(r, uid)
    _cache_put(key, r)
    logging.warning("ASK_EVENT %s", json.dumps({
        "user": pid(uid), "cached": False, "seconds": r["seconds"], "status": r["status"], "usd": r["usd"],
        "tools": [c["tool"] for c in r["tool_calls"]], "quotes": len(r["quotes"]),
        "unverified_quotes": sum(1 for x in r["quotes"] if not x["verified"]),
        **{f"t_{k}": v for k, v in (r.get("timing") or {}).items()}}))
    return _json(r)


# ------------------------------------------------------------------ manual keying baseline (/manual)
# A person keys a loan document's values by hand, timed, so the AI process has a human baseline to compare against:
# error rate against the answer key and seconds per field (src/turnaround.py builds the tables and views).
MANUAL_PAGE = (Path(__file__).parent / "static" / "manual.html").read_text()
FIELD_ORDER = ["note_date", "original_principal", "note_rate_pct", "monthly_payment", "maturity_date",
               "current_upb", "next_due_date", "appraised_value"]


def _comparison(cur) -> list[dict]:
    cur.execute("SELECT process, fields, model_error_rate, final_error_rate, straight_through_rate, "
                "human_seconds_per_field, timed_samples FROM dash.v_ai_vs_manual ORDER BY sort_order")
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


@app.route(route="manual", methods=["GET"])
def manual(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse(MANUAL_PAGE, mimetype="text/html",
                             headers={"Cache-Control": "no-store", "Content-Security-Policy": CSP,
                                      "X-Content-Type-Options": "nosniff"})


@app.route(route="api/manual/next", methods=["GET"])
def manual_next(req: func.HttpRequest) -> func.HttpResponse:
    from keying import LABELS, is_date
    who = pid(req.headers.get("x-ms-client-principal-id"))
    cur = _conn().cursor()
    cur.execute("SELECT COUNT(DISTINCT doc_id) FROM eval.field_truth")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT doc_id) FROM review.manual_entry WHERE entered_by = ?", who)
    done = cur.fetchone()[0]
    cur.execute("""SELECT TOP 1 t.doc_id FROM (SELECT DISTINCT doc_id FROM eval.field_truth) t
                   WHERE t.doc_id NOT IN (SELECT doc_id FROM review.manual_entry WHERE entered_by = ?)
                   ORDER BY NEWID()""", who)
    row = cur.fetchone()
    out = {"done": done, "total": total, "comparison": _comparison(cur), "doc": None}
    if row:
        doc_id = row[0]
        cur.execute("SELECT page_image FROM review.document WHERE doc_id = ?", doc_id)
        img = cur.fetchone()
        cur.execute("SELECT field FROM eval.field_truth WHERE doc_id = ?", doc_id)
        fields = sorted((r[0] for r in cur.fetchall()), key=lambda f: FIELD_ORDER.index(f) if f in FIELD_ORDER else 99)
        out["doc"] = {"doc_id": doc_id, "image": img[0] if img else None,
                      "fields": [{"field": f, "label": LABELS.get(f, f), "date": is_date(f)} for f in fields]}
    return _json(out)


@app.route(route="api/manual", methods=["POST"])
def manual_save(req: func.HttpRequest) -> func.HttpResponse:
    from keying import parse
    who = pid(req.headers.get("x-ms-client-principal-id"))
    try:
        body = req.get_json()
    except ValueError:
        return _json({"error": "send JSON"}, 400)
    doc_id = str(body.get("doc_id") or "")[:16]
    session = re.sub(r"[^0-9a-fA-F-]", "", str(body.get("session_id") or ""))[:36]
    values = body.get("values") or {}
    try:
        seconds = int(body.get("seconds"))
    except (TypeError, ValueError):
        return _json({"error": "missing timer"}, 400)
    if not session or not isinstance(values, dict) or not 1 <= seconds <= 3600:
        return _json({"error": "bad request (session, values, or a time over an hour)"}, 400)
    cur = _conn().cursor()
    cur.execute("SELECT field FROM eval.field_truth WHERE doc_id = ?", doc_id)
    fields = [r[0] for r in cur.fetchall()]
    if not fields:
        return _json({"error": "unknown document"}, 400)
    cur.execute("SELECT COUNT(*) FROM review.manual_entry WHERE doc_id = ? AND entered_by = ?", doc_id, who)
    if cur.fetchone()[0]:
        return _json({"error": "you already keyed this document"}, 409)
    try:
        parsed = {f: parse(f, values.get(f)) for f in fields}
    except ValueError as e:
        return _json({"error": str(e)}, 400)
    cur.executemany("INSERT INTO review.manual_entry (session_id, doc_id, field, entered_value, seconds_on_doc, entered_by) "
                    "VALUES (?, ?, ?, ?, ?, ?)", [(session, doc_id, f, v, seconds, who) for f, v in parsed.items()])
    cur.connection.commit()
    logging.warning("MANUAL_EVENT %s", json.dumps({"user": who, "doc": doc_id, "fields": len(fields), "seconds": seconds}))
    return _json({"saved": len(fields), "comparison": _comparison(cur)})
