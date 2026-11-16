"""Write docs/SCHEMA.md from the live Azure SQL database: every table, view, procedure and trigger, its columns,
keys, row count, and the repo file that creates it. Read-only against the database.

    python tools/schema_doc.py            # writes docs/SCHEMA.md
    python tools/schema_doc.py --stdout   # print instead

Rerun after adding or changing a table so the document matches what's deployed.
"""
import argparse
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from common import connect_sql, load_env  # noqa: E402

OUT = ROOT / "docs" / "SCHEMA.md"

SCHEMAS = {
    "desk": "The loan tape: the system of record. Files → documents → extracted values → one current value per loan "
            "per field (`loan_field`, system-versioned so every correction keeps the old value, who changed it and why).",
    "review": "What the Power Apps review app and the web pages read and write: the review queue, its priority order, "
              "loan-level release status, page images, and timed manual keying.",
    "eval": "The planted answer key for the synthetic loan files, used to score the model and the people.",
    "kb": "Knowledge for the Loan Desk Q&A agent (SQL full-text, no search service) and the failure-pattern analysis. "
          "Built from public FDIC data.",
    "ops": "Pipeline runs, per-run metrics, daily cost and review deadlines.",
    "ref": "Reference data.",
    "dash": "Views shaped for Power BI.",
}

CREATE = re.compile(r"CREATE\s+(?:OR\s+ALTER\s+)?(?:TABLE|VIEW|PROCEDURE|TRIGGER)\s+\[?(\w+)\]?\.\[?(\w+)", re.I)


def sources() -> dict:
    """object name -> repo file(s) whose code creates it."""
    found = {}
    for p in sorted([*ROOT.glob("sql/*.sql"), *ROOT.glob("src/*.py"), *ROOT.glob("tools/*.py")]):
        if p.name == Path(__file__).name:
            continue
        for s, o in CREATE.findall(p.read_text()):
            found.setdefault(f"{s}.{o}".lower(), []).append(p.relative_to(ROOT).as_posix())
    return found


def coltype(t, ml, pr, sc):
    if t in ("varchar", "char", "varbinary", "binary"):
        return f"{t}({'max' if ml == -1 else ml})"
    if t in ("nvarchar", "nchar"):
        return f"{t}({'max' if ml == -1 else ml // 2})"
    if t in ("decimal", "numeric"):
        return f"{t}({pr},{sc})"
    return t


def build() -> str:
    cur = connect_sql().cursor()
    cur.execute("""
        SELECT s.name, o.name, o.type, t.temporal_type, OBJECT_NAME(t.history_table_id), p.rows
        FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id
        LEFT JOIN sys.tables t ON t.object_id = o.object_id
        LEFT JOIN (SELECT object_id, SUM(rows) rows FROM sys.partitions WHERE index_id IN (0, 1)
                   GROUP BY object_id) p ON p.object_id = o.object_id
        WHERE o.type IN ('U', 'V') AND o.is_ms_shipped = 0
        ORDER BY s.name, o.type, o.name""")
    objs = cur.fetchall()
    cur.execute("""
        SELECT s.name, o.name, c.name, ty.name, c.max_length, c.precision, c.scale, c.is_nullable,
               CASE WHEN ic.column_id IS NULL THEN 0 ELSE 1 END
        FROM sys.columns c JOIN sys.objects o ON o.object_id = c.object_id
        JOIN sys.schemas s ON s.schema_id = o.schema_id JOIN sys.types ty ON ty.user_type_id = c.user_type_id
        LEFT JOIN sys.indexes i ON i.object_id = o.object_id AND i.is_primary_key = 1
        LEFT JOIN sys.index_columns ic ON ic.object_id = o.object_id AND ic.index_id = i.index_id
             AND ic.column_id = c.column_id
        WHERE o.type IN ('U', 'V') AND o.is_ms_shipped = 0 ORDER BY s.name, o.name, c.column_id""")
    cols = {}
    for s, o, n, t, ml, pr, sc, nl, pk in cur.fetchall():
        cols.setdefault((s, o), []).append((n, coltype(t, ml, pr, sc), nl, pk))
    cur.execute("""
        SELECT OBJECT_SCHEMA_NAME(f.parent_object_id), OBJECT_NAME(f.parent_object_id),
               COL_NAME(fc.parent_object_id, fc.parent_column_id),
               OBJECT_SCHEMA_NAME(f.referenced_object_id) + '.' + OBJECT_NAME(f.referenced_object_id) + '.'
               + COL_NAME(fc.referenced_object_id, fc.referenced_column_id)
        FROM sys.foreign_keys f JOIN sys.foreign_key_columns fc ON fc.constraint_object_id = f.object_id""")
    fks = {(s, o, c): ref for s, o, c, ref in cur.fetchall()}
    cur.execute("""SELECT SCHEMA_NAME(schema_id), name, type_desc FROM sys.objects
                   WHERE type IN ('P', 'TR', 'FN', 'IF', 'TF') AND is_ms_shipped = 0 ORDER BY 1, 2""")
    code = cur.fetchall()
    src = sources()

    parent_of = {(s, h): o for s, o, _, tt, h, _ in objs if tt == 2 and h}
    histories = set(parent_of)

    def made_by(s, o):
        if (s, o) in parent_of:
            return f"SQL Server (history of `{s}.{parent_of[(s, o)]}`)"
        return ", ".join(f"`{p}`" for p in src.get(f"{s}.{o}".lower(), [])) or "—"

    n_tables = sum(1 for o in objs if o[2].strip() == "U")
    n_views = len(objs) - n_tables
    lines = [
        "# Database schema (Azure SQL `loandesk`)",
        "",
        f"Generated from the live database by `tools/schema_doc.py` on {date.today().isoformat()}: "
        f"{n_tables} tables, {n_views} views, {len(code)} procedures/triggers. Row counts are as of that date. "
        "Rerun the script after changing a table; don't edit this file by hand.",
        "",
        "Design and reasoning: [DATA_MODEL.md](DATA_MODEL.md). Power BI pages: [DASHBOARD.md](DASHBOARD.md).",
        "",
        "## How the main tables connect",
        "",
        "```mermaid",
        "flowchart LR",
        "  bank --> loan_file --> document --> extraction --> loan_field",
        "  document --> text_flag",
        "  bank --> loan --> loan_field --> loan_field_history[(loan_field_history)]",
        "  loan --> review_item --> review_decision",
        "  review_item <-. item_map .-> ritem[review.item]",
        "  ritem --> item_priority[review.item_priority]",
        "  ritem -. apply_review_decisions .-> review_decision",
        "  ritem -. apply_review_decisions .-> loan_field",
        "```",
        "",
        "Every value on the loan tape traces back through `extraction_id` to the page and bounding box it was read "
        "from. A correction never overwrites: SQL Server moves the old row to the history table with its time range.",
        "",
        "## Summary",
        "",
        "| Object | Kind | Rows | Created by |",
        "|---|---|---:|---|",
    ]
    for s, o, ty, tt, h, rows in objs:
        kind = "view" if ty.strip() == "V" else ("table, system-versioned" if tt == 2 else
                                                 "history table" if tt == 1 else "table")
        lines.append(f"| `{s}.{o}` | {kind} | {'' if rows is None else f'{rows:,}'} | {made_by(s, o)} |")
    for s, n, td in code:
        lines.append(f"| `{s}.{n}` | {td.replace('SQL_', '').replace('_', ' ').lower()} | | {made_by(s, n)} |")

    current = None
    for s, o, ty, tt, h, rows in objs:
        if (s, o) in histories:
            continue
        if s != current:
            current = s
            lines += ["", f"## `{s}`", "", SCHEMAS.get(s, "")]
        kind = "View" if ty.strip() == "V" else "Table"
        extra = f" · system-versioned, history in `{s}.{h}`" if tt == 2 else ""
        count = "" if rows is None else f" · {rows:,} rows"
        lines += ["", f"### `{s}.{o}`", "", f"{kind}{count}{extra} · created by {made_by(s, o)}", "",
                  "| Column | Type | Null | Key |", "|---|---|---|---|"]
        for n, t, nl, pk in cols[(s, o)]:
            key = ["PK"] if pk else []
            if (s, o, n) in fks:
                key.append(f"→ `{fks[(s, o, n)]}`")
            lines.append(f"| `{n}` | {t} | {'yes' if nl else 'no'} | {' '.join(key)} |")
    lines += ["", "## Procedures and triggers", "", "| Name | Kind | What it does | Created by |", "|---|---|---|---|"]
    what = {
        "apply_review_decisions": "Takes decisions made in the review app (`review.item`), records them in "
                                  "`desk.review_decision`, writes corrections into `desk.loan_field` (old value goes to "
                                  "history) and logs both in `desk.audit_event`. Called by Power Automate flow 1.",
        "escalate_past_sla": "Moves open review items past their deadline to priority 0 (top of the queue) and logs it in `desk.audit_event`. Called by Power Automate flow 2.",
        "refresh_item_priority": "Ranks the review queue by exposure = chance the value is wrong × unpaid balance at stake.",
        "tr_item_priority": "Reruns the ranking when review items change.",
    }
    for s, n, td in code:
        kind = td.replace("SQL_", "").replace("_", " ").lower()
        lines.append(f"| `{s}.{n}` | {kind} | {what.get(n, '')} | {made_by(s, n)} |")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stdout", action="store_true")
    a = ap.parse_args()
    load_env()
    text = build()
    if a.stdout:
        print(text)
    else:
        OUT.write_text(text)
        print(f"wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
