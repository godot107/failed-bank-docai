"""Custom neural extraction model: training labels from the planted truth, training, and analysis.

  labels   for TRAIN-bank documents (note, payment history, appraisal), find each field's printed value in the
           Layout words (the shortest run of 1-4 words that normalizes to the truth, nearest its label) and write
           DI's labeling files, the same ones Document Intelligence Studio writes when a person labels by hand:
           docintel/neural/train/<key>.pdf, .pdf.labels.json, .pdf.ocr.json, fields.json
  build    train the model (DI reads the lake with its managed identity)
  analyze  run the model on VAL/TEST documents → samples/synthetic/docintel/extract/<model>/<key>.json

Usage: .venv/bin/python src/train_neural.py labels | build --id fbd-neural-v1 | analyze --id fbd-neural-v1
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

import ops_log
from classifier import blob_container
from common import ROOT, docintel_client, load_env
from normalize import FIELD_TYPES, FIELDS_BY_DOC, normalize, same

SYN = ROOT / "samples" / "synthetic"
PREFIX = "neural/train"
FIELDS_JSON = {
    "$schema": "https://schema.cognitiveservices.azure.com/formrecognizer/2021-03-01/fields.json",
    "fields": [{"fieldKey": f, "fieldType": "string", "fieldFormat": "not-specified"} for f in FIELD_TYPES],
    "definitions": {},
}


def center(poly):
    return sum(poly[0::2]) / 4, sum(poly[1::2]) / 4


def locate(page: dict, field: str, truth):
    """Shortest run of consecutive words whose text normalizes to the truth; ties → the one nearest a line
    containing a label-like word for the field."""
    words = page.get("words", [])
    best = None
    for n in (1, 2, 3, 4):
        for i in range(len(words) - n + 1):
            span = words[i:i + n]
            text = " ".join(w["content"] for w in span)
            if FIELD_TYPES[field] == "date" and not any(ch.isdigit() for ch in text):
                continue
            if same(field, normalize(field, text), truth):
                if FIELD_TYPES[field] == "date" and n == 1 and not any(c in text for c in "/-") and len(text) < 8:
                    continue   # a bare year/day isn't a date
                cand = (n, span, text)
                best = best or cand
        if best:
            return best[1], best[2]
    return None, None


def build_labels(dry_run: bool = False, with_aug: bool = False, prefix: str = PREFIX, skip_incomplete: bool = False) -> None:
    splits = {"train", with_aug} if with_aug else {"train"}
    rows = [r for r in csv.DictReader(open(SYN / "index.csv")) if r["split"] in splits]
    cont = None if dry_run else blob_container()
    found = missing = docs = skipped = 0
    for r in rows:
        lab = json.loads((SYN / "labels" / f"{r['loan_id']}.json").read_text())
        for d in lab["documents"]:
            if d["doc_type"] not in FIELDS_BY_DOC:
                continue
            key = f"{r['loan_id']}__{d['doc_type']}"
            layout = json.loads((SYN / "docintel" / "layout" / f"{key}.json").read_text())
            page = layout["pages"][0]
            W, H = page["width"], page["height"]
            labels = []
            for field in FIELDS_BY_DOC[d["doc_type"]]:
                truth = lab["fields"].get(field)
                if truth is None or ("printed" in d and field not in d["printed"]):
                    continue
                span, text = locate(page, field, truth)
                if not span:
                    missing += 1
                    continue
                found += 1
                boxes = [[round(v / (W if i % 2 == 0 else H), 6) for i, v in enumerate(w["polygon"])] for w in span]
                labels.append({"label": field, "value": [{"page": 1, "text": text, "boundingBoxes": boxes}]})
            if skip_incomplete and any(
                    lab["fields"].get(fl) is not None and ("printed" not in d or fl in d["printed"])
                    and fl not in {x["label"] for x in labels} for fl in FIELDS_BY_DOC[d["doc_type"]]):
                skipped += 1
                continue    # a partly labeled document teaches the model to ignore printed values
            docs += 1
            if cont:
                cont.upload_blob(f"{prefix}/{key}.pdf", (SYN / "docs" / f"{key}.pdf").read_bytes(), overwrite=True)
                cont.upload_blob(f"{prefix}/{key}.pdf.labels.json", json.dumps(
                    {"$schema": "https://schema.cognitiveservices.azure.com/formrecognizer/2021-03-01/labels.json",
                     "document": f"{key}.pdf", "labels": labels}), overwrite=True)
                cont.upload_blob(f"{prefix}/{key}.pdf.ocr.json",
                                 json.dumps({"status": "succeeded", "analyzeResult": layout}), overwrite=True)
    if cont:
        cont.upload_blob(f"{prefix}/fields.json", json.dumps(FIELDS_JSON), overwrite=True)
    print(f"{docs} train documents ({skipped} skipped as incomplete); {found} field labels located, {missing} not found"
          f"{' (dry run)' if dry_run else f' → docintel/{prefix}/'}")


def build(model_id: str, prefix: str = PREFIX) -> None:
    from azure.ai.documentintelligence import DocumentIntelligenceAdministrationClient
    from azure.ai.documentintelligence.models import AzureBlobContentSource, BuildDocumentModelRequest
    from azure.identity import DefaultAzureCredential
    load_env()
    admin = DocumentIntelligenceAdministrationClient(os.environ["DOC_INTEL_ENDPOINT"], DefaultAzureCredential())
    url = os.environ["LAKE_BLOB_ENDPOINT"].rstrip("/") + "/docintel"
    t0 = time.time()
    with ops_log.run("train_neural", model_id=model_id):
        model = admin.begin_build_document_model(BuildDocumentModelRequest(
            model_id=model_id, build_mode="neural",
            description="Loan file fields: note, payment history, appraisal (synthetic train banks)",
            azure_blob_source=AzureBlobContentSource(container_url=url, prefix=f"{prefix}/"))).result()
    print(f"built {model.model_id} in {time.time() - t0:.0f}s; fields: {sorted(model.doc_types[model_id].field_schema)}")


def analyze(model_id: str, splits: set[str], workers: int = 4) -> None:
    from azure.ai.documentintelligence.models import AnalyzeDocumentRequest
    client = docintel_client()
    out = SYN / "docintel" / "extract" / model_id
    out.mkdir(parents=True, exist_ok=True)
    todo = []
    for r in csv.DictReader(open(SYN / "index.csv")):
        if r["split"] not in splits:
            continue
        lab = json.loads((SYN / "labels" / f"{r['loan_id']}.json").read_text())
        for d in lab["documents"]:
            key = f"{r['loan_id']}__{d['doc_type']}"
            if d["doc_type"] in FIELDS_BY_DOC and not (out / f"{key}.json").exists():
                todo.append(key)
    print(f"{len(todo)} documents to analyze with {model_id} (≈ ${len(todo) * 30 / 1000:.2f} at $30/1K pages)")

    def one(key):
        res = client.begin_analyze_document(
            model_id, AnalyzeDocumentRequest(bytes_source=(SYN / "docs" / f"{key}.pdf").read_bytes())).result().as_dict()
        fields = (res.get("documents") or [{}])[0].get("fields", {})
        (out / f"{key}.json").write_text(json.dumps(
            {k: {"value": v.get("content"), "confidence": v.get("confidence")} for k, v in fields.items() if v.get("content")}))

    with ops_log.run("extract", model_id=model_id) as rec:
        rec["metrics"]["items_in"] = len(todo)
        with ThreadPoolExecutor(workers) as pool:
            for _ in pool.map(one, todo):
                rec["pages"] += 1
                rec["metrics"]["items_out"] += 1
    print(f"done → {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["labels", "build", "analyze"])
    ap.add_argument("--id", default="fbd-neural-v1")
    ap.add_argument("--splits", default="val,test")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--with-aug", nargs="?", const="train_aug", default=None,
                    help="also label augmented training banks: train_aug (default) or train_plain")
    ap.add_argument("--skip-incomplete", action="store_true", help="leave out documents where a printed value isn't found")
    ap.add_argument("--prefix", default=PREFIX, help="blob prefix for training data (v2: neural/train-v2)")
    a = ap.parse_args()
    if a.cmd == "labels":
        build_labels(a.dry_run, a.with_aug, a.prefix, a.skip_incomplete)
    elif a.cmd == "build":
        build(a.id, a.prefix)
    else:
        analyze(a.id, set(a.splits.split(",")))


if __name__ == "__main__":
    main()
