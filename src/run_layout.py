"""Split loan files into their documents and run Document Intelligence Layout on each document.

  samples/synthetic/docs/<loan_id>__<doc_type>.pdf        one PDF per document (from the labels' page ranges)
  samples/synthetic/docintel/layout/<loan_id>__<doc_type>.json   Layout result (cached; --force re-runs)

The split uses the TRUE page ranges: this is the "perfect splitter" input for extraction experiments. The
classifier (classifier.py) is evaluated separately on whole loan files.

Usage: .venv/bin/python src/run_layout.py [--splits train,val,test] [--workers 4] [--limit N]
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from pypdf import PdfReader, PdfWriter

import ops_log
from common import ROOT, docintel_client

SYN = ROOT / "samples" / "synthetic"
DOCS, LAYOUT = SYN / "docs", SYN / "docintel" / "layout"


def split_loan_file(loan_id: str, bank_id: str) -> list[tuple[str, int]]:
    """Write one PDF per document; return (doc_key, pages)."""
    label = json.loads((SYN / "labels" / f"{loan_id}.json").read_text())
    reader = PdfReader(str(SYN / "pdf" / bank_id / f"{loan_id}.pdf"))
    out = []
    DOCS.mkdir(parents=True, exist_ok=True)
    for d in label["documents"]:
        key = f"{loan_id}__{d['doc_type']}"
        path = DOCS / f"{key}.pdf"
        if not path.exists():
            w = PdfWriter()
            for p in range(d["page_from"] - 1, d["page_to"]):
                w.add_page(reader.pages[p])
            buf = io.BytesIO()
            w.write(buf)
            path.write_bytes(buf.getvalue())
        out.append((key, d["page_to"] - d["page_from"] + 1))
    return out


def analyze(client, key: str, model: str = "prebuilt-layout", features=None) -> dict:
    from azure.ai.documentintelligence.models import AnalyzeDocumentRequest
    for attempt in range(5):
        try:
            poller = client.begin_analyze_document(
                model, AnalyzeDocumentRequest(bytes_source=(DOCS / f"{key}.pdf").read_bytes()), features=features)
            return poller.result().as_dict()
        except Exception as e:
            if "429" in str(e) and attempt < 4:
                time.sleep(2 ** attempt)
                continue
            raise
    raise RuntimeError("unreachable")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--splits", default="train,val,test")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--doc-types", help="only these document types, e.g. promissory_note,servicing_history")
    a = ap.parse_args()
    splits = set(a.splits.split(","))
    rows = [r for r in csv.DictReader(open(SYN / "index.csv")) if r["split"] in splits]
    if a.limit:
        rows = rows[: a.limit]
    todo = []
    for r in rows:
        for key, pages in split_loan_file(r["loan_id"], r["bank_id"]):
            if a.doc_types and key.split("__")[1] not in a.doc_types.split(","):
                continue
            if a.force or not (LAYOUT / f"{key}.json").exists():
                todo.append((key, pages))
    pages = sum(p for _, p in todo)
    print(f"{len(todo)} documents, {pages} pages to analyze (≈ ${pages * 10 / 1000:.2f} at $10/1K pages)")
    if not todo:
        return
    LAYOUT.mkdir(parents=True, exist_ok=True)
    client = docintel_client()
    with ops_log.run("layout", model_id="prebuilt-layout") as rec:
        rec["metrics"]["items_in"] = len(todo)
        with ThreadPoolExecutor(a.workers) as pool:
            futs = {pool.submit(analyze, client, k): (k, p) for k, p in todo}
            for i, fut in enumerate(as_completed(futs), 1):
                k, p = futs[fut]
                try:
                    (LAYOUT / f"{k}.json").write_text(json.dumps(fut.result()))
                    rec["pages"] += p
                    rec["metrics"]["items_out"] += 1
                except Exception as e:
                    rec["metrics"]["di_errors"] += 1
                    print(f"  {k}: {e.__class__.__name__}: {str(e)[:120]}")
                if i % 100 == 0:
                    print(f"  {i}/{len(todo)}")
    print(f"done: {rec['metrics']['items_out']} ok, {rec['metrics']['di_errors']} errors, {rec['pages']} pages")


if __name__ == "__main__":
    main()
