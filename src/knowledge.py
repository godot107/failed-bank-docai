"""Load the Loan Desk Q&A knowledge into SQL (schema: sql/005_knowledge.sql).

What the agent can read, all with a source it can cite:
  kb.passage  loan-file pages      synthetic loan files of the five banks in the loan view (Document Intelligence
                                   Layout text, already cached by src/run_layout.py), one row per document page
              P&A agreement pages  real purchase-and-assumption agreements (the pages src/pa_terms.py read)
              Resolutions Handbook the FDIC's public handbook on how failed banks are resolved (Document Intelligence
                                   Layout on the PDF, cached in data/public/handbook/), split by section
              FDIC pages          loan sales, asset-sales FAQ, borrower's guide, when a bank fails, pre-failure loan
                                   pools, failing-bank acquisitions, bid disclosure (HTML, by heading)
              Crisis and Response  the FDIC's history of 2008-2013: overview, ch. 1 origins, ch. 4 supervision,
                                   ch. 6 resolutions and receiverships (Document Intelligence Layout, by section)
  kb.failure  every bank failure since 1934 (data/public/failures.json, FDIC BankFind; assistance transactions excluded)
  kb.deal     the winning bid on each sampled 2009-2011 failure (FDIC bid summaries, as printed)

Usage: .venv/bin/python src/knowledge.py            (idempotent: reloads every table; ~5 cents of DI the first time)
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, connect_sql, docintel_client  # noqa: E402
from fdic_sources import CRISIS_RESPONSE, FDIC_KNOWLEDGE, PUBLIC, SOURCES, USER_AGENT  # noqa: E402

LAYOUT = ROOT / "samples" / "synthetic" / "docintel" / "layout"
PA_LAYOUT = ROOT / "samples" / "synthetic" / "docintel" / "pa_terms" / "layout"
HANDBOOK = PUBLIC / "handbook"
HANDBOOK_TITLE = "FDIC Resolutions Handbook"
FDIC_DIR = PUBLIC / "fdic_knowledge"
CRISIS_TITLE = "Crisis and Response: An FDIC History, 2008-2013"
MAX_CHARS = 1500     # passage size: big enough for a full thought, small enough to quote and to rank well

DOC_LABELS = {"promissory_note": "Promissory note", "security_instrument": "Mortgage / deed of trust",
              "appraisal_1004": "Appraisal", "loan_application_1003": "Loan application",
              "settlement_statement": "Settlement statement", "servicing_history": "Servicing history",
              "servicer_comments": "Servicer comment log", "borrower_letter": "Borrower letter",
              "credit_memo": "Credit memo"}


def doc_label(doc_type: str) -> str:
    return DOC_LABELS.get(doc_type, doc_type.replace("_", " ").capitalize())


def is_noise(text: str) -> bool:
    """Bibliography entries, chart axes and tables of numbers: text that ranks on keywords but answers nothing."""
    words = text.split()
    if not words:
        return True
    urls = len(re.findall(r"https?://|www\.", text))
    numeric = sum(1 for w in words if re.fullmatch(r"[\d$%.,()\-–]+", w)) / len(words)
    cites = len(re.findall(r"\b(19|20)\d{2}\.\s", text)) + text.count("Accessed")
    return urls >= 2 or numeric > 0.35 or (cites >= 3 and urls >= 1) or text.lstrip().startswith(("Figure ", "Table ", "Source:"))


def chunk(paragraphs: list[str], max_chars: int = MAX_CHARS) -> list[str]:
    """Greedy packing of paragraphs into passages of at most max_chars (a longer paragraph is split on sentences)."""
    out, cur = [], ""
    for p in paragraphs:
        p = " ".join(p.split())
        if not p:
            continue
        pieces = [p] if len(p) <= max_chars else re.split(r"(?<=[.;:])\s+", p)
        for piece in pieces:
            if cur and len(cur) + 1 + len(piece) > max_chars:
                out.append(cur)
                cur = ""
            cur = f"{cur} {piece}".strip() if cur else piece[:max_chars]
    if cur:
        out.append(cur)
    return out


# ---------------------------------------------------------------- sources
def page_texts(layout: dict) -> dict[int, str]:
    """Document Intelligence result → {page number: text in reading order (lines)}."""
    return {p["pageNumber"]: "\n".join(ln["content"] for ln in p.get("lines", [])) for p in layout.get("pages", [])}


def loan_file_passages(loans: list[tuple]) -> list[tuple]:
    rows = []
    for loan_id, file_key, loan_number, bank in loans:
        for f in sorted(LAYOUT.glob(f"{file_key}__*.json")):
            doc_type = f.stem.split("__", 1)[1]
            for page, text in page_texts(json.loads(f.read_text())).items():
                for part in chunk(text.split("\n\n") if "\n\n" in text else [text]):
                    rows.append(("loan_file", file_key, loan_id, bank, doc_type, page,
                                 f"{doc_label(doc_type)} · loan {loan_number}", None, part))
    return rows


def handbook_layout() -> dict:
    """Download the handbook and run Document Intelligence Layout once (cached; ~32 pages ≈ $0.32 at S0)."""
    HANDBOOK.mkdir(parents=True, exist_ok=True)
    pdf, cache = HANDBOOK / "resolutions-handbook.pdf", HANDBOOK / "layout.json"
    if cache.exists():
        return json.loads(cache.read_text())
    if not pdf.exists():
        import requests
        r = requests.get(SOURCES["resolutions_handbook_pdf"], headers={"User-Agent": USER_AGENT}, timeout=120)
        r.raise_for_status()
        pdf.write_bytes(r.content)
    from azure.ai.documentintelligence.models import AnalyzeDocumentRequest
    res = docintel_client().begin_analyze_document(
        "prebuilt-layout", AnalyzeDocumentRequest(bytes_source=pdf.read_bytes())).result().as_dict()
    cache.write_text(json.dumps(res))
    return res


def handbook_passages(layout: dict, title: str = HANDBOOK_TITLE, url: str | None = None, source: str = "handbook",
                      ref: str = "resolutions-handbook") -> list[tuple]:
    """Paragraphs grouped by page and section heading; running headers, footers, page numbers and footnotes dropped.
    Used for the Resolutions Handbook and for Crisis and Response chapters (page = printed PDF page of that file)."""
    groups: list[tuple[int, str, list[str]]] = []   # (page, section, paragraphs)
    section = title
    for para in layout.get("paragraphs", []):
        role, text = para.get("role"), para.get("content", "").strip()
        page = (para.get("boundingRegions") or [{}])[0].get("pageNumber")
        if not text or page is None or role in ("pageHeader", "pageFooter", "pageNumber", "footnote"):
            continue
        if role in ("title", "sectionHeading"):
            section = text
            continue
        if groups and groups[-1][0] == page and groups[-1][1] == section:
            groups[-1][2].append(text)
        else:
            groups.append((page, section, [text]))
    rows = []
    for page, section, paras in groups:
        for part in chunk(paras):
            if len(part) < 40 or is_noise(part):   # diagram labels, captions, reference lists, chart axes
                continue
            head = title if section == title else f"{title} · {section}"
            rows.append((source, ref, None, None, None, page, head[:200],
                         url or SOURCES["resolutions_handbook_pdf"], part))
    return rows


def _get(url: str) -> bytes:
    import requests
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=120)
    r.raise_for_status()
    return r.content


def fdic_page_passages() -> list[tuple]:
    """FDIC web pages → passages grouped by the page's own headings (main content only; nav and footer dropped)."""
    from bs4 import BeautifulSoup
    FDIC_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for title, url in FDIC_KNOWLEDGE.items():
        cache = FDIC_DIR / (re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") + ".html")
        if not cache.exists():
            cache.write_bytes(_get(url))
        soup = BeautifulSoup(cache.read_text(encoding="utf-8", errors="ignore"), "html.parser")
        main = soup.find("main") or soup.find(attrs={"role": "main"}) or soup.body
        for bad in main.find_all(["nav", "script", "style", "header", "footer", "form", "aside"]):
            bad.decompose()
        section, paras, groups = title, [], []
        for el in main.find_all(["h1", "h2", "h3", "h4", "p", "li", "td", "dd", "dt"]):
            text = " ".join(el.get_text(" ", strip=True).split())
            if not text or el.find_parent(["li", "td"]) is not None and el.name in ("p",):
                continue
            if el.name in ("h1", "h2", "h3", "h4"):
                if paras:
                    groups.append((section, paras))
                section, paras = text, []
            else:
                paras.append(text)
        if paras:
            groups.append((section, paras))
        for section, ps in groups:
            for part in chunk(ps):
                if len(part) < 60 or is_noise(part):
                    continue
                head = title if section == title else f"{title} · {section}"
                rows.append(("fdic", re.sub(r"[^a-z0-9]+", "-", title.lower())[:40], None, None, None, 1,
                             f"FDIC · {head}"[:200], url, part))
    return rows


def crisis_layout(name: str, url: str) -> dict:
    """Document Intelligence Layout on one chapter (cached; ~$0.01/page at S0)."""
    FDIC_DIR.mkdir(parents=True, exist_ok=True)
    slug = url.rsplit("/", 1)[1].removesuffix(".pdf")
    pdf, cache = FDIC_DIR / f"{slug}.pdf", FDIC_DIR / f"{slug}.layout.json"
    if cache.exists():
        return json.loads(cache.read_text())
    if not pdf.exists():
        pdf.write_bytes(_get(url))
    from azure.ai.documentintelligence.models import AnalyzeDocumentRequest
    res = docintel_client().begin_analyze_document(
        "prebuilt-layout", AnalyzeDocumentRequest(bytes_source=pdf.read_bytes())).result().as_dict()
    cache.write_text(json.dumps(res))
    return res


def crisis_passages() -> list[tuple]:
    rows = []
    for name, url in CRISIS_RESPONSE.items():
        for r in handbook_passages(crisis_layout(name, url), title=f"{CRISIS_TITLE} · {name}", url=url,
                                   source="fdic", ref="crisis-response"):
            rows.append(r)
    return rows


def failures() -> list[dict]:
    return json.loads((PUBLIC / "failures.json").read_text())


def _date(s: str) -> str:
    m, d, y = s.split("/")
    return f"{y}-{int(m):02d}-{int(d):02d}"


def pa_passages(fails: dict[int, dict]) -> list[tuple]:
    rows = []
    for f in sorted(PA_LAYOUT.glob("*.json")):
        cert = int(f.stem)
        bank = (fails.get(cert) or {}).get("NAME", str(cert)).title()
        links = PUBLIC / "banks" / str(cert) / "links.json"
        url = json.loads(links.read_text()).get("pa_agreement") if links.exists() else None
        for page, text in sorted(json.loads(f.read_text()).items(), key=lambda kv: int(kv[0])):
            for part in chunk(text.split("\n")):
                rows.append(("pa_agreement", str(cert), None, bank, None, int(page),
                             f"Purchase and assumption agreement · {bank}", url, part))
    return rows


def failure_rows() -> list[tuple]:
    rows = []
    for r in failures():
        if r.get("RESTYPE") != "FAILURE":   # the dataset also has open-bank assistance transactions (e.g. 2008-09)
            continue
        def n(k):   # FDIC reports some amounts with decimals ($ thousands, e.g. COST 28965.429)
            try:
                return None if r.get(k) in (None, "") else int(round(float(r[k])))
            except (TypeError, ValueError):
                return None
        acq = (r.get("BIDNAME") or "").strip() or None
        rows.append((int(r["ID"]), n("CERT"), r["NAME"].title(), (r.get("CITY") or "").title() or None,
                     (r.get("PSTALP") or None), _date(r["FAILDATE"]), int(r["FAILYR"]), r.get("RESTYPE1") or None,
                     acq.title() if acq else None, n("QBFASSET"), n("QBFDEP"), n("COST")))
    return rows


def deal_rows(fails: dict[int, dict]) -> list[tuple]:
    import html
    rows = []
    for bid_file in sorted((PUBLIC / "banks").glob("*/bid.json")):
        cert = int(bid_file.parent.name)
        bid, f = json.loads(bid_file.read_text()), fails.get(cert, {})
        raw = {k: html.unescape(v) if isinstance(v, str) else v for k, v in bid["raw"].items()}
        links_f = bid_file.parent / "links.json"
        links = json.loads(links_f.read_text()) if links_f.exists() else {}
        buyer = re.sub(r"^\s*Winning bid(?: and bidder)?:\s*", "", raw.get("Bidder") or raw.get("Bid") or "", flags=re.I)
        loss_share = "loss share" in (bid.get("transaction", "") + " " + " ".join(raw)).lower() and any(
            "loss share" in k.lower() and str(v).strip().upper() not in ("N/A", "", "NONE") for k, v in raw.items())
        rows.append((cert, (f.get("NAME") or str(cert)).title(), (f.get("CITY") or "").title() or None,
                     f.get("PSTALP"), _date(f["FAILDATE"]) if f.get("FAILDATE") else None, buyer.strip() or None,
                     bid.get("transaction"), raw.get("Deposit Premium %"), raw.get("Asset Premium/(Discount) $(000)"),
                     raw.get("SF Loss Share Tranche 1"), raw.get("Commercial Loss Share Tranche 1"), loss_share,
                     links.get("bid_summary"), links.get("pa_agreement")))
    return rows


# ---------------------------------------------------------------- load
def run_sql_file(conn, path: Path) -> None:
    conn.autocommit = True   # full-text catalog/index DDL can't run inside a transaction
    cur = conn.cursor()
    for batch in re.split(r"^\s*GO\s*$", path.read_text(), flags=re.M | re.I):
        if batch.strip():
            cur.execute(batch)
    conn.autocommit = False


def load(conn, table: str, cols: list[str], rows: list[tuple], identity: bool = False) -> None:
    cur = conn.cursor()
    cur.execute(f"DELETE FROM {table}")
    if identity:
        cur.execute(f"DBCC CHECKIDENT ('{table}', RESEED, 0)")
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
    for i in range(0, len(rows), 500):
        cur.executemany(sql, rows[i:i + 500])
    conn.commit()
    print(f"{table}: {len(rows)} rows")


def main() -> None:
    conn = connect_sql()
    run_sql_file(conn, ROOT / "sql" / "005_knowledge.sql")
    cur = conn.cursor()
    cur.execute("SELECT loan_id, file_key, loan_number, bank FROM review.loan ORDER BY loan_id")
    loans = [tuple(r) for r in cur.fetchall()]
    fails = {int(r["CERT"]): r for r in failures() if str(r.get("CERT") or "").isdigit()}
    passages = (loan_file_passages(loans) + pa_passages(fails) + handbook_passages(handbook_layout())
                + fdic_page_passages() + crisis_passages())
    load(conn, "kb.passage", ["source", "ref", "loan_id", "bank", "doc_type", "page", "title", "url", "text"],
         passages, identity=True)
    load(conn, "kb.failure", ["failure_id", "cert", "bank", "city", "state", "fail_date", "fail_year", "resolution",
                              "acquirer", "assets_k", "deposits_k", "cost_k"], failure_rows())
    load(conn, "kb.deal", ["cert", "bank", "city", "state", "fail_date", "buyer", "transaction_type", "deposit_premium",
                           "asset_discount_k", "sf_loss_share", "commercial_loss_share", "loss_share",
                           "bid_summary_url", "pa_agreement_url"], deal_rows(fails))
    for _ in range(40):   # full-text population runs in the background; wait so the agent can search right away
        cur.execute("SELECT FULLTEXTCATALOGPROPERTY('kb_ft', 'PopulateStatus'), "
                    "(SELECT COUNT(*) FROM kb.passage)")
        status, _n = cur.fetchone()
        if status == 0:
            break
        time.sleep(3)
    cur.execute("SELECT source, COUNT(*) FROM kb.passage GROUP BY source ORDER BY source")
    print("passages:", {s: n for s, n in cur.fetchall()})
    cur.execute("SELECT COUNT(*) FROM CONTAINSTABLE(kb.passage, text, 'receivership')")
    print("full-text check ('receivership'):", cur.fetchone()[0], "passages")


if __name__ == "__main__":
    main()
