"""Document Intelligence custom classifier: split a whole loan file into documents and label each one.

  build  upload TRAIN-bank documents (one PDF per document, by type) to docintel/classifier/train/<type>/
         and build the classifier (DI reads the lake with its managed identity; no keys or SAS)
  eval   classify whole VAL/TEST loan files with split="auto"; compare to the true documents:
         page accuracy (each page gets the right type) and document accuracy (exact type + page range)

Usage: .venv/bin/python src/classifier.py build --id fbd-classifier-v1
       .venv/bin/python src/classifier.py eval  --id fbd-classifier-v1 [--splits val,test]
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import os
import time

import ops_log
from common import ROOT, docintel_client, load_env

SYN = ROOT / "samples" / "synthetic"
PREFIX = "classifier/train"


def blob_container(name: str = "docintel", endpoint_var: str = "LAKE_BLOB_ENDPOINT"):
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobServiceClient
    load_env()
    return BlobServiceClient(os.environ[endpoint_var], credential=DefaultAzureCredential()).get_container_client(name)


def build(model_id: str, splits: tuple[str, ...] = ("train",)) -> None:
    from azure.ai.documentintelligence import DocumentIntelligenceAdministrationClient
    from azure.ai.documentintelligence.models import (AzureBlobContentSource, BuildDocumentClassifierRequest,
                                                      ClassifierDocumentTypeDetails)
    from azure.identity import DefaultAzureCredential

    from azure.storage.blob import ContentSettings
    train = [r for r in csv.DictReader(open(SYN / "index.csv")) if r["split"] in splits]
    # classifier training data goes to the plain-blob training account: classifier builds can't read the HNS lake
    cont = blob_container("training", "TRAIN_BLOB_ENDPOINT")
    counts = collections.Counter()
    for r in train:
        lab = json.loads((SYN / "labels" / f"{r['loan_id']}.json").read_text())
        for d in lab["documents"]:
            key = f"{r['loan_id']}__{d['doc_type']}"
            if not (SYN / "docintel" / "layout" / f"{key}.json").exists():
                continue
            cont.upload_blob(f"{PREFIX}/{d['doc_type']}/{key}.pdf", (SYN / "docs" / f"{key}.pdf").read_bytes(), overwrite=True,
                             content_settings=ContentSettings(content_type="application/pdf"))
            # GOTCHA (2026-11-11): the classifier build reports "TrainingContentMissing" unless each PDF has its
            # Layout result next to it as <file>.pdf.ocr.json (what Document Intelligence Studio writes). Storage
            # type, identity vs SAS, prefix vs file list and content type were all ruled out; this was the fix.
            layout = json.loads((SYN / "docintel" / "layout" / f"{key}.json").read_text())
            cont.upload_blob(f"{PREFIX}/{d['doc_type']}/{key}.pdf.ocr.json",
                             json.dumps({"status": "succeeded", "analyzeResult": layout}), overwrite=True,
                             content_settings=ContentSettings(content_type="application/json"))
            # Found 2026-11-11: builds fail with TrainingContentMissing unless each PDF has its Layout result next to
            # it (<file>.pdf.ocr.json, the same file Document Intelligence Studio writes). We already have them.
            layout = SYN / "docintel" / "layout" / f"{key}.json"
            cont.upload_blob(f"{PREFIX}/{d['doc_type']}/{key}.pdf.ocr.json",
                             json.dumps({"status": "succeeded", "analyzeResult": json.loads(layout.read_text())}),
                             overwrite=True, content_settings=ContentSettings(content_type="application/json"))
            counts[d["doc_type"]] += 1
    print("uploaded:", dict(counts))
    types = [t for t, n in counts.items() if n >= 5]
    load_env()
    admin = DocumentIntelligenceAdministrationClient(os.environ["DOC_INTEL_ENDPOINT"], DefaultAzureCredential())
    url = os.environ["TRAIN_BLOB_ENDPOINT"].rstrip("/") + "/training"
    t0 = time.time()
    with ops_log.run("train_classifier", model_id=model_id) as rec:
        rec["metrics"]["items_in"] = sum(counts[t] for t in types)
        poller = admin.begin_build_classifier(BuildDocumentClassifierRequest(
            classifier_id=model_id, description="Loan file document types (synthetic train banks)",
            doc_types={t: ClassifierDocumentTypeDetails(azure_blob_source=AzureBlobContentSource(
                container_url=url, prefix=f"{PREFIX}/{t}/")) for t in types}))
        res = poller.result()
    print(f"built {res.classifier_id} in {time.time() - t0:.0f}s: {sorted(res.doc_types)}")


def evaluate(model_id: str, splits: set[str]) -> None:
    from azure.ai.documentintelligence.models import ClassifyDocumentRequest

    client = docintel_client()
    rows = [r for r in csv.DictReader(open(SYN / "index.csv")) if r["split"] in splits]
    out_dir = SYN / "docintel" / "classify" / model_id
    out_dir.mkdir(parents=True, exist_ok=True)
    page_ok = page_n = doc_ok = doc_n = 0
    by_split = collections.defaultdict(collections.Counter)
    confusion = collections.Counter()
    with ops_log.run("classify", model_id=model_id) as rec:
        for r in rows:
            lab = json.loads((SYN / "labels" / f"{r['loan_id']}.json").read_text())
            cache = out_dir / f"{r['loan_id']}.json"
            if cache.exists():
                result = json.loads(cache.read_text())
            else:
                pdf = (SYN / "pdf" / r["bank_id"] / f"{r['loan_id']}.pdf").read_bytes()
                poller = client.begin_classify_document(model_id, ClassifyDocumentRequest(bytes_source=pdf), split="auto")
                result = poller.result().as_dict()
                cache.write_text(json.dumps(result))
                rec["pages"] += int(r["pages"])
            rec["metrics"]["items_in"] += 1
            pred_pages = {}
            pred_docs = set()
            for d in result.get("documents", []):
                pages = sorted({b["pageNumber"] for b in d.get("boundingRegions", [])})
                if pages:
                    pred_docs.add((d["docType"], pages[0], pages[-1]))
                    for p in range(pages[0], pages[-1] + 1):
                        pred_pages[p] = d["docType"]
            for d in lab["documents"]:
                doc_n += 1
                hit = (d["doc_type"], d["page_from"], d["page_to"]) in pred_docs
                doc_ok += hit
                by_split[lab["split"]]["docs"] += 1
                by_split[lab["split"]]["docs_ok"] += hit
                for p in range(d["page_from"], d["page_to"] + 1):
                    page_n += 1
                    got = pred_pages.get(p, "(none)")
                    page_ok += got == d["doc_type"]
                    by_split[lab["split"]]["pages"] += 1
                    by_split[lab["split"]]["pages_ok"] += got == d["doc_type"]
                    if got != d["doc_type"]:
                        confusion[(d["doc_type"], got)] += 1
    print(f"page accuracy {page_ok / page_n:.1%} ({page_ok}/{page_n}); document accuracy (type + exact pages) "
          f"{doc_ok / doc_n:.1%} ({doc_ok}/{doc_n})")
    for s, c in sorted(by_split.items()):
        print(f"  {s}: pages {c['pages_ok'] / c['pages']:.1%}, documents {c['docs_ok'] / c['docs']:.1%}")
    if confusion:
        print("  most common confusions (true → predicted):", confusion.most_common(6))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["build", "eval"])
    ap.add_argument("--id", default="fbd-classifier-v1")
    ap.add_argument("--splits", default="val,test")
    ap.add_argument("--train-splits", default="train", help="training splits, e.g. train,train_hard")
    a = ap.parse_args()
    build(a.id, tuple(a.train_splits.split(","))) if a.cmd == "build" else evaluate(a.id, set(a.splits.split(",")))


if __name__ == "__main__":
    main()
