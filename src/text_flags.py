"""U2: risk flags from free text (servicer comment logs, borrower letters, credit memos).

Text comes from the Document Intelligence Layout output (OCR), not from the generator, so OCR noise on scanned
pages is part of the test. Each document is cut into segments (one comment-log entry, or one paragraph). A segment
is labeled with a flag when it matches a planted flag sentence, as a decoy when it matches a decoy sentence, and
"none" otherwise.

Methods (keywords and tfidf use TRAIN + TRAIN_AUG banks, whose notes are written in training styles S1/S2):
  keywords  a lexicon a person would write from the training notes
  tfidf     word + character n-gram TF-IDF, one logistic regression per flag
  llm       zero-shot gpt-4.1-mini (Foundry, Entra auth) with the flag definitions only; one call per document;
            every flag must name the segment it comes from (cached in samples/synthetic/docintel/llm_flags/)
Evaluated per flag on the VALIDATION (S3) and TEST (S4, S5) banks, whose note-writing styles are unseen.
Every positive prediction carries its segment text as the quote, and the quote is checked against the page text.

Usage: .venv/bin/python src/text_flags.py
"""
from __future__ import annotations

import collections
import csv
import difflib
import json
import re

from pydantic import ValidationError

from common import ROOT
from schemas import TextFlagReply

SYN = ROOT / "samples" / "synthetic"
TEXT_DOCS = ("servicer_comments", "borrower_letter", "credit_memo")
FLAGS = ["bankruptcy", "litigation", "hardship", "fraud_suspected", "property_damage", "payoff_request",
         "underwriting_exception", "draw_without_inspection"]

KEYWORDS = {  # written from the TRAINING notes only (styles S1, S2)
    "bankruptcy": [r"\bchapter (7|11|13)\b", r"\bch ?(7|11|13)\b", r"bankruptcy (notice|petition)", r"automatic stay",
                   r"\bbk hold\b", r"filed (under|bankruptcy)"],
    "litigation": [r"\blawsuit\b", r"\bsuit filed\b", r"litigation pending", r"foreclosure action", r"contesting",
                   r"disputing the foreclosure", r"\bmy attorney\b", r"filed a complaint"],
    "hardship": [r"forbearance", r"\bmodification\b", r"hardship", r"lost (his|her|their)? ?job", r"reduced hours",
                 r"defer .*payments", r"fallen behind"],
    "fraud_suspected": [r"fraud", r"misrepresentation", r"straw buyer", r"inflated", r"altered", r"suspicious activity"],
    "property_damage": [r"\bdamage\b", r"\bvacant\b", r"broken windows", r"insurance claim", r"adjuster", r"unoccupied"],
    "payoff_request": [r"payoff", r"refinanc"],
    "underwriting_exception": [r"exception to policy", r"approved despite", r"exceeding policy"],
    "draw_without_inspection": [r"before the site inspection", r"without a current inspection"],
}
NEGATION = re.compile(r"\b(no|not|neg|negative|clear|clean|unrelated|none)\b", re.I)


def norm(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9%#' ]", " ", t.lower())).strip()


def segments(layout: dict, doc_type: str) -> list[dict]:
    """Comment-log rows (the Comment column) or paragraphs."""
    segs = []
    if doc_type == "servicer_comments":
        for t in layout.get("tables", []):
            for c in t["cells"]:
                if c["columnIndex"] == 2 and c.get("kind") != "columnHeader" and c["rowIndex"] > 0:
                    segs.append(c.get("content", ""))
    else:
        for p in layout.get("paragraphs", []):
            if len(p["content"]) > 25 and p.get("role") not in ("title", "sectionHeading", "pageFooter", "pageHeader"):
                segs.append(p["content"])
    return [{"text": s} for s in segs if s.strip()]


def match(a: str, b: str) -> bool:
    a, b = norm(a), norm(b)
    return bool(a) and bool(b) and (b in a or a in b or difflib.SequenceMatcher(None, a, b).ratio() > 0.8)


def build_dataset() -> list[dict]:
    rows = []
    for r in csv.DictReader(open(SYN / "index.csv")):
        lab = json.loads((SYN / "labels" / f"{r['loan_id']}.json").read_text())
        for d in lab["documents"]:
            if d["doc_type"] not in TEXT_DOCS:
                continue
            f = SYN / "docintel" / "layout" / f"{r['loan_id']}__{d['doc_type']}.json"
            if not f.exists():
                continue
            layout = json.loads(f.read_text())
            page_text = norm(layout.get("content", ""))
            planted = [x for x in lab["flags"] if x["doc_type"] == d["doc_type"]]
            decoys = [x for x in lab["decoys"] if x["doc_type"] == d["doc_type"]]
            for s in segments(layout, d["doc_type"]):
                flags = sorted({x["flag"] for x in planted if match(s["text"], x["quote"])})
                rows.append({"loan_id": r["loan_id"], "bank_id": r["bank_id"], "split": r["split"], "style": r["style"],
                             "doc_type": d["doc_type"], "text": s["text"], "flags": flags,
                             "decoy": any(match(s["text"], x["quote"]) for x in decoys),
                             "scanned": d["scanned"], "page_text": page_text})
    return rows


def keyword_predict(text: str) -> set[str]:
    out = set()
    for f, pats in KEYWORDS.items():
        if any(re.search(p, text, re.I) for p in pats):
            if f in ("bankruptcy", "litigation", "fraud_suspected") and NEGATION.search(text):
                continue    # "no bk on file", "no suits on title"
            out.add(f)
    return out


def tfidf_models(train: list[dict]):
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import FeatureUnion
    vec = FeatureUnion([("w", TfidfVectorizer(ngram_range=(1, 2), min_df=1, sublinear_tf=True)),
                        ("c", TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), min_df=2, sublinear_tf=True))])
    X = vec.fit_transform([r["text"] for r in train])
    models = {}
    for f in FLAGS:
        y = [f in r["flags"] for r in train]
        if sum(y) >= 2:
            models[f] = LogisticRegression(max_iter=2000, class_weight="balanced", C=4.0).fit(X, y)
    return vec, models


FLAG_DEFS = {
    "bankruptcy": "the borrower has filed for or is in bankruptcy (not a search that found no filing)",
    "litigation": "a lawsuit, foreclosure contest, or attorney dispute involving this loan (not unrelated suits, not a clean search)",
    "hardship": "the borrower reports financial hardship or asks for forbearance, deferral, modification or payment relief",
    "fraud_suspected": "possible fraud, identity problems, inflated value or misrepresentation (not a clean fraud screen)",
    "property_damage": "physical damage to the collateral, or it is vacant/vandalized",
    "payoff_request": "a payoff quote/statement was requested or the borrower plans to refinance or sell",
    "underwriting_exception": "the loan was approved as an exception to policy or despite a stated deficiency",
    "draw_without_inspection": "a construction draw was funded before or without an inspection",
}


def llm_predict(rows: list[dict]) -> list[set[str]]:
    """One call per document: numbered segments in, {segment number: [flags]} out. Cached by document."""
    import os
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from openai import AzureOpenAI
    from common import load_env
    load_env()
    client = AzureOpenAI(azure_endpoint=os.environ["OPENAI_ENDPOINT"], api_version="2024-10-21",
                         azure_ad_token_provider=get_bearer_token_provider(
                             DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"))
    cache_dir = SYN / "docintel" / "llm_flags"
    cache_dir.mkdir(parents=True, exist_ok=True)
    by_doc = collections.defaultdict(list)
    for i, r in enumerate(rows):
        by_doc[(r["loan_id"], r["doc_type"])].append(i)
    preds = [set() for _ in rows]
    system = ("You review loan servicing documents for a bank receivership. For each numbered segment, list which "
              "risk flags it clearly states. Definitions:\n" + "\n".join(f"- {k}: {v}" for k, v in FLAG_DEFS.items())
              + "\nOnly flag what the segment itself states. Negative statements (e.g. a search that found nothing) "
              "are not flags. Reply as JSON: {\"flags\": [{\"segment\": <number>, \"flags\": [<flag names>]}]} "
              "listing only segments with at least one flag.")
    usage = collections.Counter()
    for (loan_id, doc_type), idx in by_doc.items():
        cache = cache_dir / f"{loan_id}__{doc_type}.json"
        if cache.exists():
            reply = TextFlagReply.model_validate_json(cache.read_text())     # typed: unknown flag names are rejected
        else:
            user = "\n".join(f"[{n}] {rows[i]['text']}" for n, i in enumerate(idx, 1))
            resp = client.chat.completions.create(
                model="gpt-4.1-mini", temperature=0, response_format={"type": "json_object"},
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
            usage["in"] += resp.usage.prompt_tokens
            usage["out"] += resp.usage.completion_tokens
            try:
                reply = TextFlagReply.model_validate_json(resp.choices[0].message.content)
            except ValidationError as e:          # malformed answer: no flags from this document, and say so
                print(f"  {loan_id} {doc_type}: invalid LLM reply ({e.error_count()} errors); treated as no flags")
                reply = TextFlagReply(flags=[])
            cache.write_text(reply.model_dump_json())
        for item in reply.flags:
            if item.segment <= len(idx):
                preds[idx[item.segment - 1]] |= {f for f in item.flags if f in FLAG_DEFS}
    if usage:
        # gpt-4.1-mini list price: $0.40 / 1M input, $1.60 / 1M output tokens
        print(f"  llm tokens: {usage['in']} in, {usage['out']} out ≈ ${usage['in'] * 0.4e-6 + usage['out'] * 1.6e-6:.3f}")
    return preds


def score(rows: list[dict], preds: list[set[str]]) -> dict:
    out = {}
    for f in FLAGS:
        tp = sum(f in p and f in r["flags"] for r, p in zip(rows, preds))
        fp = sum(f in p and f not in r["flags"] for r, p in zip(rows, preds))
        fn = sum(f not in p and f in r["flags"] for r, p in zip(rows, preds))
        out[f] = {"tp": tp, "fp": fp, "fn": fn, "support": tp + fn,
                  "precision": tp / (tp + fp) if tp + fp else None, "recall": tp / (tp + fn) if tp + fn else None}
    tp, fp, fn = (sum(v[k] for v in out.values()) for k in ("tp", "fp", "fn"))
    out["ALL"] = {"tp": tp, "fp": fp, "fn": fn, "support": tp + fn, "precision": tp / (tp + fp) if tp + fp else None,
                  "recall": tp / (tp + fn) if tp + fn else None}
    decoy_fp = sum(bool(p) and r["decoy"] and not r["flags"] for r, p in zip(rows, preds))
    out["decoys_flagged"] = {"n": sum(r["decoy"] for r in rows), "flagged": decoy_fp}
    out["quotes_verified"] = sum(norm(r["text"]) in r["page_text"] for r, p in zip(rows, preds) if p) / max(1, sum(bool(p) for p in preds))
    return out


def hybrid(rows: list[dict], kw: list[set[str]], tf: list[set[str]], llm: list[set[str]]) -> dict:
    """Routing rule: a flag the LLM raises AND a precise method (keywords or TF-IDF) agrees with is auto-accepted;
    a flag only the LLM raises goes to a person (with its quoted segment). Measures what each lane costs."""
    auto = [l & (k | t) for k, t, l in zip(kw, tf, llm)]
    review = [l - a for a, l in zip(auto, llm)]
    n_auto = sum(len(a) for a in auto)
    auto_ok = sum(len(a & set(r["flags"])) for a, r in zip(auto, rows))
    n_rev = sum(len(x) for x in review)
    rev_ok = sum(len(x & set(r["flags"])) for x, r in zip(review, rows))
    truth = sum(len(r["flags"]) for r in rows)
    return {"auto_flags": n_auto, "auto_precision": auto_ok / n_auto if n_auto else None,
            "review_flags": n_rev, "review_true": rev_ok, "review_false": n_rev - rev_ok,
            "share_to_review": n_rev / (n_auto + n_rev) if n_auto + n_rev else None,
            "recall_after_review": (auto_ok + rev_ok) / truth if truth else None,
            "wrong_auto_accepted": n_auto - auto_ok}


def fmt(x):
    return "  —  " if x is None else f"{x:5.0%}"


def main() -> None:
    rows = build_dataset()
    train = [r for r in rows if r["split"] in ("train", "train_aug")]
    evals = {s: [r for r in rows if r["split"] == s] for s in ("val", "test")}
    print(f"segments: train {len(train)} ({sum(bool(r['flags']) for r in train)} flagged), "
          + ", ".join(f"{s} {len(v)} ({sum(bool(r['flags']) for r in v)} flagged)" for s, v in evals.items()))
    vec, models = tfidf_models(train)
    results = {}
    for s, ev in evals.items():
        kw = [keyword_predict(r["text"]) for r in ev]
        X = vec.transform([r["text"] for r in ev])
        probs = {f: m.predict_proba(X)[:, 1] for f, m in models.items()}
        tf = [{f for f in models if probs[f][i] >= 0.5} for i in range(len(ev))]
        llm = llm_predict(ev)
        results[s] = {"keywords": score(ev, kw), "tfidf": score(ev, tf), "llm": score(ev, llm),
                      "hybrid": hybrid(ev, kw, tf, llm)}
    out = SYN / "eval" / "text_flags.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=1))
    for s, by in results.items():
        print(f"\n== {s}")
        print(f"  {'flag':<24} {'n':>3} | {'keywords P':>10} {'R':>5} | {'tfidf P':>8} {'R':>5} | {'llm P':>6} {'R':>5}")
        for f in FLAGS + ["ALL"]:
            k, t, m = by["keywords"][f], by["tfidf"][f], by["llm"][f]
            if k["support"] or k["fp"] or t["fp"] or m["fp"]:
                print(f"  {f:<24} {k['support']:>3} | {fmt(k['precision']):>10} {fmt(k['recall']):>5} | "
                      f"{fmt(t['precision']):>8} {fmt(t['recall']):>5} | {fmt(m['precision']):>6} {fmt(m['recall']):>5}")
        h = by["hybrid"]
        print(f"  hybrid: {h['auto_flags']} auto-accepted (precision {fmt(h['auto_precision'])}, wrong {h['wrong_auto_accepted']}); "
              f"{h['review_flags']} to a person ({h['review_true']} real, {h['review_false']} false alarms; "
              f"{fmt(h['share_to_review'])} of flags); recall after review {fmt(h['recall_after_review'])}")
        for m in ("keywords", "tfidf", "llm"):
            d = by[m]["decoys_flagged"]
            print(f"  {m}: decoy sentences flagged {d['flagged']}/{d['n']}; quotes found verbatim in page text "
                  f"{by[m]['quotes_verified']:.0%}")


if __name__ == "__main__":
    main()
