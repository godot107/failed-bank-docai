"""Register review.loan, review.loan_detail and review.item_priority as SQL data sources in the canvas app source (powerapps/src).

The canvas source keeps, per SQL table: DataSources/<name>.json (connection + column mapping),
pkgs/TableDefinitions/<name>.json (the connector's table metadata), an entry in Connections/Connections.json,
and an order slot in Entropy/Entropy.json. Writing them by hand is error-prone, so this reads the columns from
the live database (INFORMATION_SCHEMA) and writes the same shapes the maker portal writes for review.item.
It also adds the loan screen to CanvasManifest.json's ScreenOrder. Idempotent.

Usage: .venv/bin/python powerapps/add_loan_view.py      (then powerapps/deploy.sh install)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "src"
sys.path.insert(0, str(HERE.parent / "src"))
from common import connect_sql  # noqa: E402

# name → writable in the app's metadata. Both are registered read-write like every other table here (a read-only
# registration made the player return no rows); what the app can actually change is set by the SQL grants
# (src/loan_view.py: SELECT on loan_detail, UPDATE on the four release columns of loan only).
TABLES = {"review.loan": True, "review.loan_detail": True, "review.item_priority": True}
SCREEN = "LoanScreen"
STRING_FN = ["lt", "le", "eq", "ne", "gt", "ge", "min", "max", "countdistinct", "contains", "startswith", "endswith",
             "length", "indexof", "replace", "substring", "substringof", "tolower", "toupper", "trim", "concat"]
INT_FN = ["add", "average", "countdistinct", "div", "eq", "ge", "gt", "le", "lt", "max", "min", "mod", "mul", "ne",
          "negate", "sub", "sum"]
FLOAT_FN = ["lt", "le", "eq", "ne", "gt", "ge", "min", "max", "countdistinct", "add", "sub", "mul", "div", "round",
            "floor", "ceiling", "negate", "sum", "average"]
DATE_FN = ["lt", "le", "eq", "ne", "gt", "ge", "min", "max", "countdistinct", "day", "month", "year", "hour", "minute",
           "second", "date", "time"]
TABLE_CAPS = json.loads(json.loads((SRC / "pkgs/TableDefinitions/review.item.json").read_text())
                        ["DataEntityMetadataJson"]["[review].[item]"])["x-ms-capabilities"]


def column(name: str, dtype: str, maxlen, identity: bool, pk: bool, writable_table: bool, generated: bool) -> dict:
    perm = "read-only" if (identity or generated or not writable_table) else "read-write"
    if dtype in ("nvarchar", "varchar", "nchar", "char"):
        p = {"type": "string", "x-ms-capabilities": {"filterFunctions": STRING_FN}}
        if maxlen and maxlen > 0:
            p["maxLength"] = maxlen
    elif dtype in ("int", "smallint", "tinyint"):
        p = {"type": "integer", "format": "int32", "minimum": -2147483648, "maximum": 2147483647,
             "x-ms-capabilities": {"filterFunctions": INT_FN}}
    elif dtype == "bigint":
        p = {"type": "integer", "format": "int64", "x-ms-capabilities": {"filterFunctions": INT_FN}}
    elif dtype in ("float", "real", "decimal", "numeric"):
        p = {"type": "number", "format": "double", "minimum": -1.7976931348623157e+308,
             "maximum": 1.7976931348623157e+308, "x-ms-capabilities": {"filterFunctions": FLOAT_FN}}
    elif dtype in ("datetime2", "datetime", "date"):
        p = {"type": "string", "format": "date-no-tz", "x-ms-capabilities": {"filterFunctions": DATE_FN}}
    elif dtype == "bit":
        p = {"type": "boolean", "x-ms-capabilities": {"filterFunctions": ["eq", "ne", "not", "and", "or"]}}
    else:
        raise SystemExit(f"unmapped SQL type {dtype} for {name}")
    p = {"title": name, **p, "x-ms-permission": perm, "x-ms-sort": "none"}
    if pk:
        p.update({"x-ms-keyOrder": 1, "x-ms-keyType": "primary"})
    return p


def main() -> None:
    cur = connect_sql().cursor()
    manifest = json.loads((SRC / "Connections/Connections.json").read_text())
    conn_entry = next(iter(manifest.values()))
    dataset = next(iter(conn_entry["datasets"]))
    template = json.loads((SRC / "DataSources/review.item.json").read_text())[0]
    entropy = json.loads((SRC / "Entropy/Entropy.json").read_text())

    for table, writable in TABLES.items():
        schema, name = table.split(".")
        cur.execute("""SELECT c.name, t.name, c.max_length, c.is_identity, c.generated_always_type,
                              CASE WHEN ic.column_id IS NULL THEN 0 ELSE 1 END, c.is_nullable
                       FROM sys.columns c JOIN sys.types t ON t.user_type_id = c.user_type_id
                       LEFT JOIN sys.indexes i ON i.object_id = c.object_id AND i.is_primary_key = 1
                       LEFT JOIN sys.index_columns ic ON ic.object_id = c.object_id AND ic.index_id = i.index_id
                                                     AND ic.column_id = c.column_id
                       WHERE c.object_id = OBJECT_ID(?) ORDER BY c.column_id""", (table,))
        cols = cur.fetchall()
        if not cols:
            raise SystemExit(f"{table} not found: run src/loan_view.py (and src/prescribe.py) first")
        props, required = {}, []
        for cname, dtype, maxlen, identity, generated, pk, nullable in cols:
            ml = maxlen // 2 if dtype.startswith("n") and maxlen > 0 else maxlen
            props[cname] = column(cname, dtype, ml, bool(identity), bool(pk), writable, bool(generated))
            if not nullable and not identity and not generated:
                required.append(cname)
        meta = {"name": f"[{schema}].[{name}]", "title": f"[{schema}].[{name}]",
                "x-ms-permission": "read-write" if writable else "read-only", "x-ms-capabilities": TABLE_CAPS,
                "schema": {"type": "array", "items": {"type": "object", "required": required, "properties": props},
                           "x-ms-permission": "read-write" if writable else "read-only"}}
        (SRC / "pkgs/TableDefinitions" / f"{table}.json").write_text(json.dumps(
            {"DataEntityMetadataJson": {f"[{schema}].[{name}]": json.dumps(meta, separators=(",", ":"))},
             "EntityName": table, "TableName": f"[{schema}].[{name}]"}, indent=2))
        ds = dict(template)
        ds.update({"Name": table, "IsWritable": writable,
                   "ConnectedDataSourceInfoNameMapping": {c[0]: c[0] for c in cols}})
        (SRC / "DataSources" / f"{table}.json").write_text(json.dumps([ds], indent=2))
        conn_entry["datasets"][dataset]["dataSources"][table] = {"tableName": f"[{schema}].[{name}]"}
        if table not in conn_entry["dataSources"]:
            conn_entry["dataSources"].append(table)
        order = entropy.setdefault("OrderDataSource", {})
        order.setdefault(table, max(order.values(), default=-1) + 1)
        print(f"{table}: {len(cols)} columns ({'read-write' if writable else 'read-only'})")

    (SRC / "Connections/Connections.json").write_text(json.dumps(manifest, indent=2))
    (SRC / "Entropy/Entropy.json").write_text(json.dumps(entropy, indent=2))
    cm = json.loads((SRC / "CanvasManifest.json").read_text())
    if SCREEN not in cm["ScreenOrder"]:
        cm["ScreenOrder"].append(SCREEN)
    (SRC / "CanvasManifest.json").write_text(json.dumps(cm, indent=2))
    print(f"ScreenOrder: {cm['ScreenOrder']}")


if __name__ == "__main__":
    main()
