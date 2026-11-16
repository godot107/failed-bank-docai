"""Self-test of desk.apply_review_decisions against the live database, inside a transaction that is ROLLED BACK.

Scenarios (docs/REVIEW.md D2-D4):
  1. correct a number            → decision 'correct', loan-tape value = the new number, source = reviewer
  2. correct a date with garbage → item goes back to 'open' with a message; nothing written to the loan tape
  3. not on document             → loan-tape value cleared (NULL), source = reviewer
  4. accept                      → decision 'confirm', value unchanged
  5. change of mind after apply  → a second decision is recorded and the loan-tape value follows it
  6. run twice                   → second run applies nothing

Usage: .venv/bin/python tools/selftest_apply.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from common import connect_sql  # noqa: E402


def one(cur, sql, *args):
    cur.execute(sql, args)
    return cur.fetchone()


def main() -> None:
    conn = connect_sql()
    conn.autocommit = False
    cur = conn.cursor()
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        ok &= bool(cond)
        print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")

    try:
        def pick(field_type, used):
            return one(cur, f"""SELECT TOP 1 i.item_id, m.loan_id, m.field FROM review.item i JOIN review.item_map m
                                ON m.item_id = i.item_id WHERE i.status = 'open' AND m.field_type = ?
                                AND i.item_id NOT IN ({','.join(map(str, used)) or '0'}) ORDER BY i.item_id""", field_type)
        used = []
        num = pick("number", used); used.append(num[0])
        bad = pick("date", used); used.append(bad[0])
        nod = pick("number", used); used.append(nod[0])
        acc = pick("number", used); used.append(acc[0])

        def decide(item, status, value):
            cur.execute("UPDATE review.item SET status = ?, reviewed_value = ?, reviewer = 'selftest', "
                        "reviewed_at = SYSUTCDATETIME() WHERE item_id = ?", (status, value, item))

        decide(num[0], "corrected", 123456.0)
        decide(bad[0], "corrected", 2008046.0)          # 7 digits: not a YYYYMMDD date
        decide(nod[0], "not_on_document", None)
        decide(acc[0], "accepted", None)
        cur.execute("EXEC desk.apply_review_decisions")
        applied = cur.fetchone()[0]
        check("first run applies the 3 valid decisions", applied == 3, f"applied={applied}")

        v = one(cur, "SELECT value, source FROM desk.loan_field WHERE loan_id = ? AND field = ?", num[1], num[2])
        check("correction becomes the loan-tape value", v and v[0] == "123456.0000" and v[1] == "reviewer", str(v))
        h = one(cur, "SELECT COUNT(*) FROM desk.loan_field FOR SYSTEM_TIME ALL WHERE loan_id = ? AND field = ?", num[1], num[2])
        check("model's value kept in history", h[0] >= 2, f"versions={h[0]}")
        st = one(cur, "SELECT status, comment FROM review.item WHERE item_id = ?", bad[0])
        check("invalid date goes back to the reviewer", st[0] == "open" and "YYYYMMDD" in (st[1] or ""), str(st))
        bv = one(cur, "SELECT source FROM desk.loan_field WHERE loan_id = ? AND field = ?", bad[1], bad[2])
        check("invalid date not written to the loan tape", not bv or bv[0] != "reviewer", str(bv))
        nv = one(cur, "SELECT value, source FROM desk.loan_field WHERE loan_id = ? AND field = ?", nod[1], nod[2])
        check("not-on-document clears the value", nv and nv[0] is None and nv[1] == "reviewer", str(nv))
        ad = one(cur, "SELECT d.decision FROM desk.review_decision d JOIN review.item_map m ON m.desk_item_id = d.item_id "
                      "WHERE m.item_id = ?", acc[0])
        check("accept recorded as confirm", ad and ad[0] == "confirm", str(ad))

        cur.execute("EXEC desk.apply_review_decisions")
        again = cur.fetchone()[0]
        check("second run applies nothing", again == 0, f"applied={again}")

        decide(num[0], "corrected", 654321.0)            # change of mind after the decision was applied
        cur.execute("EXEC desk.apply_review_decisions")
        cur.fetchone()
        v2 = one(cur, "SELECT value FROM desk.loan_field WHERE loan_id = ? AND field = ?", num[1], num[2])
        n_dec = one(cur, "SELECT COUNT(*) FROM desk.review_decision d JOIN review.item_map m ON m.desk_item_id = d.item_id "
                         "WHERE m.item_id = ?", num[0])
        check("change of mind is applied as a new decision", v2[0] == "654321.0000" and n_dec[0] == 2,
              f"value={v2[0]} decisions={n_dec[0]}")
    finally:
        conn.rollback()
        print("rolled back (live data unchanged)")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
