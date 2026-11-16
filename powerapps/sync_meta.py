"""Copy the app's SQL data sources (src/Connections/Connections.json) into a solution's canvas-app meta.xml.

A canvas app in a solution carries its connection references twice: inside the .msapp (from src/) and in
CanvasApps/<app>.meta.xml (<ConnectionReferences>, JSON). The player resolves tables from the meta.xml copy, so a
table added in source but missing there fails at runtime with "Connection not configured for this data source".
deploy.sh runs this on the solution folder before packing (install and push).

Usage: python3 powerapps/sync_meta.py <solution folder>
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main(folder: str) -> None:
    src = json.loads((HERE / "src/Connections/Connections.json").read_text())
    for meta in Path(folder).glob("CanvasApps/*.meta.xml"):
        text = meta.read_text()
        m = re.search(r"<ConnectionReferences>(.*?)</ConnectionReferences>", text, re.S)
        if not m:
            continue
        refs = json.loads(m.group(1))
        changed = []
        for conn_id, conn in src.items():
            if conn_id not in refs:
                continue
            ref = refs[conn_id]
            for name in conn.get("dataSources", []):
                if name not in ref.setdefault("dataSources", []):
                    ref["dataSources"].append(name)
                    changed.append(name)
            for ds_name, ds in conn.get("datasets", {}).items():
                target = ref.setdefault("dataSets", {}).setdefault(ds_name, {}).setdefault("dataSources", {})
                for name, spec in ds.get("dataSources", {}).items():
                    target.setdefault(name, spec)
        if changed:
            new = json.dumps(refs, separators=(",", ":"))
            meta.write_text(text[:m.start(1)] + new + text[m.end(1):])
        print(f"{meta.name}: {'added ' + ', '.join(changed) if changed else 'up to date'}")


if __name__ == "__main__":
    main(sys.argv[1])
