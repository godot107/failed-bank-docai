"""U3: key terms from REAL public purchase-and-assumption (P&A) agreements, scored against FDIC's bid summaries.

For each 2008-2012 failed bank with both documents (data/public/banks/<cert>/):
  1. truth     the bank's public bid summary: buyer, transaction type, deposit premium %, asset premium/(discount)
               $(000), single-family and commercial loss-share tranche-1 %; + failure date from the failures API
  2. pages     choose the pages worth analyzing from the PDF's existing (free) OCR text layer: the cover page(s),
               Article VII "Bid; Initial Payment", and the first "Applicable Percentage" page of each loss-share
               agreement. Typically 4-6 of 60-170 pages.
  3. layout    Document Intelligence Layout on just those pages (2-page chunks, page numbers mapped back)
  4. extract   (a) regex baseline over the Layout text; (b) gpt-4.1-mini with JSON output where EVERY value must
               carry a verbatim quote; quotes are checked against the Layout text
  5. score     per field, against the bid summary

Usage: .venv/bin/python src/pa_terms.py [--limit N] [--no-llm]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
from difflib import SequenceMatcher
from pathlib import Path

import requests

import di_chunk as dc
import ops_log
from schemas import PATerms
from common import ROOT, docintel_client, load_env

PUBLIC = ROOT / "data" / "public"
OUT = ROOT / "samples" / "synthetic" / "docintel" / "pa_terms"      # caches (gitignored with docintel/)
EVAL = ROOT / "samples" / "synthetic" / "eval"
UA = {"User-Agent": "failed-bank-docai/0.1 (+contact@willieman.com)"}
FIELDS = ["buyer", "effective_date", "deposit_premium_pct", "asset_premium_discount_usd", "loss_share",
          "sf_loss_share_pct", "commercial_loss_share_pct"]


# ---------------------------------------------------------------- truth
def bid_summary(cert: int, url: str) -> dict | None:
    path = PUBLIC / "banks" / str(cert) / "bid.json"
    if path.exists():
        return json.loads(path.read_text())
    html = requests.get(url, headers=UA, timeout=60).text
    i = html.find("<table")
    if i < 0:
        return None
    rows = re.findall(r"<tr.*?</tr>", html[i:html.find("</table>", i)], re.S)
    cells = [[re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).replace("&nbsp;", "").strip()
              for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", r, re.S)] for r in rows]
    head = cells[0]
    win = next((r for r in cells[1:] if r and r[0].lower().startswith("winning")), None)
    if not win:
        return None
    rec = dict(zip(head, win))
    out = {"buyer": re.sub(r"^Winning bid and bidder:\s*", "", rec.get("Bidder", "")).split(",")[0].strip(),
           "transaction": rec.get("Type of Transaction", ""), "raw": rec}
    path.write_text(json.dumps(out, indent=1))
    return out


def pct(s: str | None) -> float | None:
    m = re.search(r"-?\d+(\.\d+)?", s or "")
    return float(m.group()) if m else None


def usd_thousands(s: str | None) -> float | None:
    """'$ (34,750)' → -34750000.0 (bid summaries are in $000; parentheses = discount)."""
    if not s or not re.search(r"\d", s):
        return None
    v = float(re.sub(r"[^\d.]", "", s))
    return -v * 1000 if "(" in s else v * 1000


def truth_for(cert: int, fail: dict, bid: dict) -> dict:
    import html as _html
    r = bid["raw"]
    buyer = re.sub(r"^\s*Winning bid(?: and bidder)?:\s*", "", _html.unescape(r.get("Bidder") or r.get("Bid") or ""), flags=re.I)
    buyer = re.split(r",|\(", buyer)[0].strip()
    standard = "SF Loss Share Tranche 1" in r          # some large-bank summaries use other tables (e.g. "Loss Share Pool A")
    sf = pct(r.get("SF Loss Share Tranche 1")) if standard else "unknown"
    com = pct(r.get("Commercial Loss Share Tranche 1")) if standard else "unknown"
    any_ls = "loss share" in (bid["transaction"] + " " + " ".join(r.keys())).lower()
    m, d, y = fail["FAILDATE"].split("/")
    return {"buyer": buyer, "effective_date": f"{y}-{int(m):02d}-{int(d):02d}",
            "deposit_premium_pct": pct(r.get("Deposit Premium %")),
            "asset_premium_discount_usd": usd_thousands(r.get("Asset Premium/(Discount) $(000)")),
            "loss_share": any_ls, "sf_loss_share_pct": sf, "commercial_loss_share_pct": com}


# ---------------------------------------------------------------- pages + layout
def choose_pages(texts: list[str]) -> list[int]:
    pages = {1, 2}
    # the bid sentence itself (Article VII is titled "Bid; Initial Payment" in later versions, "First Loss Tranche"
    # in 2009-2010 versions, so match the sentence, not the title)
    for i, t in enumerate(texts, 1):
        if i > 8 and re.search(r"submitted to the Receiver\s+(?:an?\s+|the\s+)?(?:positive\s+)?(?:asset|deposit)", t, re.I):
            pages.add(i)
            break
    seen = set()
    for i, t in enumerate(texts, 1):
        for kind in ("single family", "commercial"):
            if kind not in seen and i > 8 and re.search(kind + r".{0,80}shared.loss", t, re.I | re.S) and \
                    re.search(r"(Applicable Percentage|\b(80|95|70|60)\s*(%|percent)|eighty percent)", t, re.I):
                pages.add(i)
                seen.add(kind)
    return sorted(pages)


def layout_text(cert: int, pdf: Path, pages: list[int], client) -> dict[int, str]:
    from azure.ai.documentintelligence.models import AnalyzeDocumentRequest
    cache = OUT / "layout" / f"{cert}.json"
    if cache.exists():
        return {int(k): v for k, v in json.loads(cache.read_text()).items()}
    text: dict[int, str] = {}
    with ops_log.run("pa_layout", model_id="prebuilt-layout") as rec:
        for ch in dc.make_chunks(pdf, pages, size=2):
            res = client.begin_analyze_document("prebuilt-layout", AnalyzeDocumentRequest(bytes_source=ch.pdf)).result().as_dict()
            for p in res.get("pages", []):
                text[ch.pages[p["pageNumber"] - 1]] = "\n".join(ln["content"] for ln in p.get("lines", []))
            rec["pages"] += len(ch.pages)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(text))
    return text


# ---------------------------------------------------------------- extraction
def regex_extract(text: str) -> dict:
    flat = re.sub(r"\s+", " ", text)
    out = {}
    m = re.search(r"AMONG .{0,400}?AND\s+(.+?)\s+(?:[A-Z ]+,\s+[A-Z ]+\s+)?(?:DATED|EFFECTIVE)", flat)
    if m:
        out["buyer"] = m.group(1)
    m = re.search(r"EFFECTIVE AS OF\s+([A-Z][a-z]+ \d{1,2}, \d{4})", flat, re.I)
    if m:
        out["effective_date"] = m.group(1)
    m = re.search(r"Deposit premium bid of\s+([\d.]+)\s*%", flat, re.I)
    if m:
        out["deposit_premium_pct"] = m.group(1)
    m = re.search(r"asset (?:premium \(discount\)|discount|premium) bid of\s+(\(?\$?\s*\(?[\d,]+(?:\.\d+)?\)?)", flat, re.I)
    if m:
        out["asset_premium_discount_usd"] = m.group(1)
    out["loss_share"] = bool(re.search(r"Shared-Loss Agreement|Loss Share", flat, re.I))
    for key, kind in (("sf_loss_share_pct", "Single Family"), ("commercial_loss_share_pct", "Commercial")):
        m = re.search(kind + r".{0,600}?Applicable Percentage.{0,200}?\((\d+)%\)", flat, re.I)
        if m:
            out[key] = m.group(1)
    return out


SYSTEM = ("You extract key terms from a failed-bank Purchase and Assumption Agreement. Text from selected pages is "
          "given with [page N] markers. For every term give the value, the exact text from the pages that states it "
          "(copied verbatim), and its page. Use null when the pages don't state a value. Never compute or guess a "
          "number. Effective date as YYYY-MM-DD; asset premium positive, asset discount negative, in dollars; loss-share "
          "percentages are the first Applicable Percentage of each shared-loss agreement.")


def llm_extract(cert: int, pages_text: dict[int, str]) -> dict:
    """gpt-4.1-mini with STRUCTURED OUTPUT: the reply must match schemas.PATerms (every term = value + verbatim quote +
    page). Pydantic validates it at the boundary; an unquoted value is dropped. Returns the flat dict main() scores."""
    cache = OUT / "llm_structured" / f"{cert}.json"
    if cache.exists():
        terms = PATerms.model_validate_json(cache.read_text())
    else:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        from openai import AzureOpenAI
        load_env()
        client = AzureOpenAI(azure_endpoint=os.environ["OPENAI_ENDPOINT"], api_version="2024-10-21",
                             azure_ad_token_provider=get_bearer_token_provider(
                                 DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"))
        user = "\n\n".join(f"[page {p}]\n{t}" for p, t in sorted(pages_text.items()))
        parse = getattr(client.chat.completions, "parse", None) or client.beta.chat.completions.parse
        resp = parse(model="gpt-4.1-mini", temperature=0, response_format=PATerms,
                     messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}])
        terms = resp.choices[0].message.parsed
        if terms is None:
            raise RuntimeError(f"{cert}: model refused or returned no parsable answer")
        terms = PATerms.model_validate(terms.model_dump())          # re-run validators (unquoted values dropped)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(terms.model_dump_json(indent=1))
    flat: dict = {}
    for name in PATerms.model_fields:
        q = getattr(terms, name)
        flat[name], flat[f"{name}_quote"], flat[f"{name}_page"] = q.value, q.quote, q.page
    return flat


# ---------------------------------------------------------------- scoring
def norm_name(s: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
    s = re.sub(r"\b(the|national association|n a|na|inc|bank and trust|bank|company|co|fsb|ssb)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def as_date(s) -> str | None:
    from normalize import as_date as nd
    return nd(str(s)) if s else None


def as_money(s) -> float | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    neg = "(" in s or "-" in s
    digits = re.sub(r"[^\d.]", "", s)
    return (-1 if neg else 1) * float(digits) if digits else None


def check(field: str, got, truth) -> str:
    if truth == "unknown":
        return "no_truth"
    if (got is None or got == "") and truth is None and field.endswith("_pct"):
        return "correct"          # "not stated" is the right answer when the deal has no such term
    if (got is None or got == "") and field == "loss_share" and truth is False:
        return "correct"          # null = no shared-loss agreement found
    if got is None or got == "":
        return "missing"
    if field == "buyer":
        if not truth:
            return "no_truth"
        name = re.split(r",", str(got))[0]                  # drop the city/state the agreement prints after the name
        return "correct" if SequenceMatcher(None, norm_name(name), norm_name(truth)).ratio() >= 0.8 else "wrong"
    if field == "effective_date":
        return "correct" if as_date(got) == truth else "wrong"
    if field == "loss_share":
        return "correct" if bool(got) == truth else "wrong"
    if truth is None and field in ("deposit_premium_pct", "asset_premium_discount_usd"):
        return "no_truth"                                   # the bid summary doesn't state it
    if field == "asset_premium_discount_usd":
        v = as_money(got)
        return "correct" if v is not None and truth is not None and abs(v - truth) <= max(1000, abs(truth) * 0.001) else "wrong"
    v = pct(str(got))
    if truth is None:
        return "correct" if v is None else "wrong"
    return "correct" if v is not None and abs(v - truth) < 0.01 else "wrong"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-llm", action="store_true")
    a = ap.parse_args()
    fails = {int(r["CERT"]): r for r in json.loads((PUBLIC / "failures.json").read_text()) if r.get("CERT")}
    banks = []
    for d in sorted((PUBLIC / "banks").iterdir()):
        cert = int(d.name)
        links = json.loads((d / "links.json").read_text()) if (d / "links.json").exists() else {}
        f = fails.get(cert)
        if f and 2008 <= int(f["FAILYR"]) <= 2012 and (d / "pa_agreement.pdf").exists() and links.get("bid_summary"):
            banks.append((cert, f, links["bid_summary"]))
    if a.limit:
        banks = banks[: a.limit]
    client = docintel_client()
    rows, pages_used, pages_total = [], 0, 0
    for cert, f, url in banks:
        bid = bid_summary(cert, url)
        if not bid:
            print(f"  {cert}: no winning bid row; skipped")
            continue
        truth = truth_for(cert, f, bid)
        pdf = PUBLIC / "banks" / str(cert) / "pa_agreement.pdf"
        texts = dc.page_texts(pdf)
        pages = choose_pages(texts)
        pages_used += len(pages)
        pages_total += len(texts)
        lt = layout_text(cert, pdf, pages, client)
        joined = "\n".join(lt[p] for p in sorted(lt))
        flat = re.sub(r"\s+", " ", joined)
        methods = {"regex": regex_extract(joined)}
        if not a.no_llm:
            methods["llm"] = llm_extract(cert, lt)
        for m, got in methods.items():
            for fld in FIELDS:
                q = got.get(f"{fld}_quote") if m == "llm" else None
                rows.append({"cert": cert, "bank": f["NAME"], "year": f["FAILYR"], "method": m, "field": fld,
                             "truth": truth[fld], "got": got.get(fld), "outcome": check(fld, got.get(fld), truth[fld]),
                             "quote_verified": (re.sub(r"\s+", " ", q) in flat) if q else None,
                             "pages_analyzed": len(pages), "pages_total": len(texts)})
        print(f"  {cert} {f['NAME'][:34]:<34} {f['FAILYR']} pages {len(pages)}/{len(texts)}  "
              + "  ".join(f"{m}:{sum(r['outcome'] == 'correct' for r in rows if r['cert'] == cert and r['method'] == m)}/{len(FIELDS)}"
                          for m in methods))
    EVAL.mkdir(parents=True, exist_ok=True)
    with open(EVAL / "pa_terms.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"\n{len(banks)} agreements; analyzed {pages_used} of {pages_total} pages ({pages_used / pages_total:.0%}); "
          f"Layout ≈ ${pages_used * 10 / 1000:.2f}")
    for m in sorted({r["method"] for r in rows}):
        print(f"== {m}")
        for fld in FIELDS:
            rs = [r for r in rows if r["method"] == m and r["field"] == fld]
            rs = [r for r in rs if r["outcome"] != "no_truth"]
            c = sum(r["outcome"] == "correct" for r in rs)
            wr = sum(r["outcome"] == "wrong" for r in rs)
            qv = [r["quote_verified"] for r in rs if r["quote_verified"] is not None]
            print(f"  {fld:<28} correct {c}/{len(rs)}  wrong {wr}" + (f"  quotes verified {sum(qv)}/{len(qv)}" if qv else ""))


if __name__ == "__main__":
    main()
