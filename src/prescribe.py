"""Prescriptive review: which item to work next, and which loans to finish first.

1. Queue priority (expected-value framework: Provost & Fawcett, Data Science for Business, ch. 7)
       exposure = P(value is wrong | why it was routed) x loan balance at stake
   "Expected dollars of loan balance that would reach bidders with a wrong value if nobody looked." Highest first.
   P(wrong) per routing reason is a Beta(1, 1)-smoothed rate learned from other banks' outcomes plus this bank's
   reviewer decisions so far (never this bank's evaluation answers, which are the key for its open items). Labels come from reviewer decisions when they exist (corrected /
   not_on_document = wrong, accepted = right) and otherwise from the held-out evaluation. Every decision a reviewer
   makes becomes a new label: the active-learning loop (Han, Kamber & Pei 3rd ed. p.470; Aggarwal §11.7) in its
   simplest form: the queue re-ranks itself from what reviewers find.

2. Release plan (0/1 knapsack)
   Given R reviewer-minutes before the data room opens, choose the loans to finish so the most loan balance becomes
   releasable. A loan with open items needs (open items x minutes per item); one with none is already releasable.

    .venv/bin/python src/prescribe.py --reviewer <work UPN>   # install sql/007 + grant the reviewer; check SQL = Python
    .venv/bin/python src/prescribe.py plan --minutes 60   # print a release plan
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

MINUTES_PER_ITEM = 4      # planning assumption for one review decision (look at the page, compare, save)
PRIOR = (1.0, 1.0)        # Beta(a, b) prior on P(wrong): one pseudo-wrong, one pseudo-right

LABELS_SQL = """
SELECT l.bank, i.item_id, i.flag_reasons AS reason,
       CASE WHEN i.status IN ('corrected', 'not_on_document') THEN 1 WHEN i.status = 'accepted' THEN 0
            WHEN o.outcome IS NULL THEN NULL WHEN o.outcome = 'correct' THEN 0 ELSE 1 END AS wrong,
       CASE WHEN i.status <> 'open' THEN 'reviewer' WHEN o.outcome IS NOT NULL THEN 'evaluation' END AS label_source
FROM review.item i
JOIN review.loan l ON l.file_key = LEFT(i.doc_id, LEN(l.file_key)) OR i.doc_id = CONCAT('L', l.loan_id)
LEFT JOIN eval.field_outcome o ON o.loan_id = l.loan_id AND o.field = i.field AND o.routed = 'review'
"""
OPEN_SQL = """
SELECT i.item_id, l.loan_id, l.loan_number, l.bank, i.field, i.field_label, i.flag_reasons,
       TRY_CAST(REPLACE(COALESCE(NULLIF(u.schedule_text, ''), u.value_text), ',', '') AS float) AS upb
FROM review.item i
JOIN review.loan l ON l.file_key = LEFT(i.doc_id, LEN(l.file_key)) OR i.doc_id = CONCAT('L', l.loan_id)
LEFT JOIN review.loan_detail u ON u.loan_id = l.loan_id AND u.kind = 'field' AND u.field = 'current_upb'
WHERE i.status = 'open'
"""
SQL_FILE = Path(__file__).resolve().parents[1] / "sql" / "007_prescriptive.sql"


def rates(labels: list[tuple], exclude_bank: str) -> dict[str, tuple[float, int, int]]:
    """reason → (smoothed P(wrong), wrong, n) from other banks' labels and this bank's reviewer decisions. Combined reasons ('a;b') also
    count toward each single reason, so a rare combination still has data."""
    counts: dict[str, list[int]] = {}
    for bank, _, reason, wrong, src in labels:
        # the item's own bank counts only through reviewers' decisions (decided items); its evaluation labels are
        # the answer key for items still open, so they are never used to score that bank
        if wrong is None or not reason or (bank == exclude_bank and src != "reviewer"):
            continue
        for key in {reason, *reason.split(";")}:
            c = counts.setdefault(key, [0, 0])
            c[0] += int(wrong)
            c[1] += 1
    a, b = PRIOR
    return {k: ((w + a) / (n + a + b), w, n) for k, (w, n) in counts.items()}


def p_wrong(reason: str, table: dict) -> tuple[float, str]:
    if reason in table and table[reason][2] >= 3:
        p, w, n = table[reason]
        return p, f"{w} wrong of {n} labelled"
    parts = [table[r] for r in reason.split(";") if r in table]
    if parts:   # no data for the exact combination: the riskiest single reason
        p, w, n = max(parts)
        return p, f"{w} wrong of {n} labelled (few labels)"
    a, b = PRIOR
    return a / (a + b), "no data yet (prior)"


def prioritize(conn) -> list[dict]:
    cur = conn.cursor()
    cur.execute(LABELS_SQL)
    labels = [tuple(r) for r in cur.fetchall()]
    cur.execute(OPEN_SQL)
    cols = [d[0] for d in cur.description]
    items = [dict(zip(cols, r)) for r in cur.fetchall()]
    cache: dict[str, dict] = {}
    for it in items:
        table = cache.setdefault(it["bank"], rates(labels, it["bank"]))
        it["p_wrong"], it["p_basis"] = p_wrong(it["flag_reasons"] or "", table)
        if (it["flag_reasons"] or "") == "missing":   # nothing was read: the tape is incomplete until someone enters it
            it["p_wrong"], it["p_basis"] = 1.0, "value missing: the tape has no value"
        it["exposure_usd"] = it["p_wrong"] * (it["upb"] or 0.0)
        it["changes_pool"] = it["field"] == "next_due_date"
    items.sort(key=lambda r: (-r["exposure_usd"], r["item_id"]))
    for i, it in enumerate(items, 1):
        it["priority_rank"] = i
    return items


def refresh(conn) -> list[dict]:
    """Install/refresh the SQL implementation (table, procedure, trigger), run it, and check it agrees with this
    Python reference on every open item (rank and P(wrong)). Returns the Python ranking."""
    import re
    conn.autocommit = True
    cur = conn.cursor()
    for batch in re.split(r"^\s*GO\s*$", SQL_FILE.read_text(), flags=re.M | re.I):
        if batch.strip():
            cur.execute(batch)
    cur.execute("EXEC review.refresh_item_priority")
    conn.autocommit = False
    cur.execute("SELECT item_id, priority_rank, p_wrong FROM review.item_priority")
    sql = {r[0]: (r[1], float(r[2])) for r in cur.fetchall()}
    items = prioritize(conn)
    diffs = [it["item_id"] for it in items
             if it["item_id"] not in sql or abs(sql[it["item_id"]][1] - round(it["p_wrong"], 4)) > 1e-4
             or sql[it["item_id"]][0] != it["priority_rank"]]
    print(f"SQL vs Python reference: {len(items) - len(diffs)}/{len(items)} items agree" +
          (f"; differ: {diffs[:10]}" if diffs else ""))
    return items


# ---------------------------------------------------------------- release plan (0/1 knapsack, exact DP)
def knapsack(loans: list[dict], capacity: int) -> list[dict]:
    """Max total balance with total minutes <= capacity. Exact dynamic program over minutes (small: tens of loans)."""
    best = [(0.0, ())] * (capacity + 1)
    for idx, ln in enumerate(loans):
        w, v = ln["minutes"], ln["upb"]
        if w > capacity:
            continue
        for c in range(capacity, w - 1, -1):
            cand = best[c - w][0] + v
            if cand > best[c][0]:
                best[c] = (cand, best[c - w][1] + (idx,))
    return [loans[i] for i in max(best, key=lambda t: t[0])[1]]


def release_plan(conn, minutes: int = 60, minutes_per_item: int = MINUTES_PER_ITEM) -> dict:
    cur = conn.cursor()
    cur.execute("""SELECT l.loan_number, l.bank, l.pool, l.release_status,
                          TRY_CAST(REPLACE(COALESCE(NULLIF(u.schedule_text, ''), u.value_text), ',', '') AS float) AS upb,
                          (SELECT COUNT(*) FROM review.item i WHERE i.status = 'open'
                              AND (LEFT(i.doc_id, LEN(l.file_key)) = l.file_key OR i.doc_id = CONCAT('L', l.loan_id))) AS open_items
                   FROM review.loan l
                   LEFT JOIN review.loan_detail u ON u.loan_id = l.loan_id AND u.kind = 'field' AND u.field = 'current_upb'""")
    cols = [d[0] for d in cur.description]
    loans = [dict(zip(cols, r)) for r in cur.fetchall()]
    for ln in loans:
        ln["upb"] = ln["upb"] or 0.0
        ln["minutes"] = ln["open_items"] * minutes_per_item
    pending = [ln for ln in loans if ln["release_status"] == "pending"]
    ready = [ln for ln in pending if ln["open_items"] == 0]
    blocked = [ln for ln in pending if ln["open_items"] > 0]
    chosen = sorted(knapsack(blocked, int(minutes)), key=lambda ln: -ln["upb"])
    blocked_upb = sum(ln["upb"] for ln in blocked)
    return {
        "reviewer_minutes": int(minutes), "minutes_per_item": minutes_per_item,
        "already_releasable": {"loans": len(ready), "balance": round(sum(ln["upb"] for ln in ready), 2)},
        "blocked": {"loans": len(blocked), "balance": round(blocked_upb, 2),
                    "minutes_to_clear_all": sum(ln["minutes"] for ln in blocked)},
        "plan": [{k: ln[k] for k in ("loan_number", "bank", "pool", "open_items", "minutes", "upb")} for ln in chosen],
        "plan_balance": round(sum(ln["upb"] for ln in chosen), 2),
        "plan_share_of_blocked_balance": round(sum(ln["upb"] for ln in chosen) / blocked_upb, 3) if blocked_upb else None,
        "method": "0/1 knapsack (exact): most loan balance made releasable within the reviewer minutes",
    }


def main() -> None:
    from common import connect_sql
    p = argparse.ArgumentParser()
    p.add_argument("cmd", nargs="?", default="refresh", choices=["refresh", "plan"])
    p.add_argument("--minutes", type=int, default=60)
    p.add_argument("--reviewer", help="work account UPN that uses the review app (gets SELECT on the priorities)")
    a = p.parse_args()
    conn = connect_sql()
    if a.cmd == "plan":
        print(json.dumps(release_plan(conn, a.minutes), indent=1, default=str))
        return
    items = refresh(conn)
    if a.reviewer:
        q = a.reviewer.replace("]", "]]")
        cur = conn.cursor()
        cur.execute(f"GRANT SELECT ON review.item_priority TO [{q}]")
        conn.commit()
    print(f"review.item_priority: {len(items)} open items ranked")
    for r in items[:8]:
        print(f"  #{r['priority_rank']:<2} {r['loan_number']} {r['field_label']:<22} {r['flag_reasons']:<22} "
              f"P(wrong) {r['p_wrong']:.2f} ({r['p_basis']}) x ${r['upb'] or 0:,.0f} = ${r['exposure_usd']:,.0f}")


if __name__ == "__main__":
    main()
