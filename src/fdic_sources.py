"""Public FDIC sources for failed-bank-docai: the single source of truth for URLs and fetchers.

Everything here is public FDIC data. Downloads go to data/public/ (gitignored) and are skipped if
they already exist. Run as a script to pull the default set:

    python src/fdic_sources.py                 # failures + failed-bank list + docs for recent failures
    python src/fdic_sources.py --docs 10       # documents for the 10 most recent failures
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "data" / "public"

# Generic client name per workspace rule (no personal details in requests).
USER_AGENT = "failed-bank-docai/0.1 (+contact@willieman.com)"

FDIC_API = "https://api.fdic.gov/banks"
SOURCES = {
    # BankFind "Failures and Assistance": one row per failure since 1934 (4,117 as of 2026-11).
    "failures_api": f"{FDIC_API}/failures",
    # Call Report financials per institution (by CERT): the failed bank's last loan mix.
    "financials_api": f"{FDIC_API}/financials",
    # Failed Bank List (since 2000-10-01): name, city, state, cert, acquirer, closing date, fund.
    "failed_bank_list_csv": "https://www.fdic.gov/bank-failures/download-data.csv",
    # Per-bank page: P&A agreement PDF, bid summary, creditor notice, FAQ, press release.
    # Recent failures use this slug pattern; 2000-2015 failures use legacy short pages
    # (/resources/resolutions/bank-failures/failed-bank-list/<short>.html), found by crawling the list.
    "bank_page": "https://www.fdic.gov/bank-failures/failed-bank-list/{slug}",
    # Paginated HTML list (25 rows/page): links every failed bank's page, new or legacy.
    "failed_bank_list_html": "https://www.fdic.gov/bank-failures/failed-bank-list?page={page}",
    # FDIC Resolutions Handbook (32 pages): resolution process, P&A transactions, loss share, receivership and
    # asset marketing. Background knowledge for the Loan Desk Q&A agent (src/knowledge.py).
    "resolutions_handbook_pdf": "https://www.fdic.gov/bank-failures/resolutions-handbook.pdf",
}
# Curated FDIC knowledge for the Loan Desk Q&A agent (src/knowledge.py): how failed-bank assets are sold, what
# borrowers are told, and the FDIC's own history of the 2008-2013 wave. Pages are HTML; the history is PDF chapters.
FDIC_KNOWLEDGE = {
    "Loan Sales": "https://www.fdic.gov/asset-sales/loan-sales",
    "Asset Sales FAQ": "https://www.fdic.gov/asset-sales/frequently-asked-questions",
    "A Borrower's Guide to an FDIC-Insured Bank Failure": "https://www.fdic.gov/bank-failures/borrowers-guide-fdic-insured-bank-failure",
    "When a Bank Fails: Facts for Depositors, Creditors and Borrowers": "https://www.fdic.gov/bank-failures/when-bank-fails-facts-depositors-creditors-and-borrowers",
    "Loan Pools Offered to Asset Buyers Prior to Bank Failure": "https://www.fdic.gov/franchise-sales/loan-pools-offered-asset-buyers-prior-bank-failure",
    "Failing Bank Acquisitions": "https://www.fdic.gov/resources/resolutions/franchise-sales/acquisition-overview.html",
    "Failed Financial Institution Bid Disclosure Policy": "https://www.fdic.gov/bank-failures/failed-financial-institution-bid-disclosure-policy",
}
CRISIS_RESPONSE = {   # Crisis and Response: An FDIC History, 2008-2013 (chapters most relevant to failed-bank work)
    "Overview": "https://www.fdic.gov/resources/publications/crisis-response/book/crisis-response-overview.pdf",
    "Ch. 1 Origins of the Crisis": "https://www.fdic.gov/resources/publications/crisis-response/book/crisis-response-chapter-1.pdf",
    "Ch. 4 Bank Supervision": "https://www.fdic.gov/resources/publications/crisis-response/book/crisis-response-chapter-4.pdf",
    "Ch. 6 Bank Resolutions and Receiverships": "https://www.fdic.gov/resources/publications/crisis-response/book/crisis-response-chapter-6.pdf",
}
FDIC_BASE = "https://www.fdic.gov"

# Call Report loan fields ($ thousands) used to size and mix the synthetic loan portfolio.
LOAN_MIX_FIELDS = {
    "LNLSNET": "net loans and leases",
    "LNRE": "real estate loans (total)",
    "LNRERES": "1-4 family residential",
    "LNRECONS": "construction and land development",
    "LNRENRES": "nonfarm nonresidential (commercial real estate)",
    "LNCI": "commercial and industrial",
    "LNCON": "consumer",
}

# Document kinds we keep from a bank's page, matched on the link URL.
DOC_PATTERNS = {
    "pa_agreement": r"purchase-(and-)?assumption|p_and_a|_pa\.pdf",
    "loss_share": r"loss-?shar|shared-?loss",
    "creditor_notice": r"publication-notice",
    "bid_summary": r"bid-summary",
    "faq": r"frequently-asked-questions",
}


def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def _get(s: requests.Session, url: str, **kw) -> requests.Response:
    for attempt in range(3):
        r = s.get(url, timeout=60, **kw)
        if r.status_code < 500:
            r.raise_for_status()
            return r
        time.sleep(2 * (attempt + 1))
    r.raise_for_status()
    return r


def slugify(name: str, amp: str = "and") -> str:
    """FDIC page slug from a bank name: 'Nano Banc' -> 'nano-banc'."""
    s = name.lower().replace("&", f" {amp} ")
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def slug_candidates(name: str) -> list[str]:
    """FDIC isn't consistent about '&' in slugs ('bank-and-trust' vs 'bank-trust')."""
    out = []
    for amp in ("and", ""):
        for n in (name, re.sub(r"^the\s+", "", name, flags=re.I)):
            sl = slugify(n, amp)
            if sl not in out:
                out.append(sl)
    return out


def fetch_failures(s: requests.Session, limit: int = 10000) -> Path:
    """All failures from the BankFind API → data/public/failures.json."""
    out = PUBLIC / "failures.json"
    if out.exists():
        return out
    r = _get(s, SOURCES["failures_api"], params={
        "limit": limit, "sort_by": "FAILDATE", "sort_order": "DESC", "format": "json"})
    rows = [d["data"] for d in r.json()["data"]]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=1))
    return out


def fetch_failed_bank_list(s: requests.Session) -> Path:
    out = PUBLIC / "failed_bank_list.csv"
    if out.exists():
        return out
    r = _get(s, SOURCES["failed_bank_list_csv"])
    if b"Bank" not in r.content[:200]:
        raise ValueError("failed bank list CSV looks wrong (no header)")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(r.content)
    return out


def load_failed_bank_list() -> dict[int, dict]:
    """Failed Bank List keyed by CERT. Full bank names (the API abbreviates them, e.g. 'FS&LA'),
    which the per-bank page slugs need. The CSV is cp1252 with non-breaking spaces in the header."""
    import csv, io
    text = (PUBLIC / "failed_bank_list.csv").read_bytes().decode("cp1252")
    rows = csv.DictReader(io.StringIO(text))
    out = {}
    for r in rows:
        r = {k.replace("\xa0", " ").strip(): (v or "").strip() for k, v in r.items()}
        out[int(r["Cert"])] = r
    return out


def fetch_loan_mix(s: requests.Session, cert: int) -> dict:
    """The bank's most recent Call Report loan mix ($ thousands)."""
    out = PUBLIC / "loan_mix" / f"{cert}.json"
    if out.exists():
        return json.loads(out.read_text())
    r = _get(s, SOURCES["financials_api"], params={
        "filters": f"CERT:{cert}", "fields": ",".join(["CERT", "REPDTE", "ASSET", *LOAN_MIX_FIELDS]),
        "sort_by": "REPDTE", "sort_order": "DESC", "limit": 1})
    data = r.json()["data"]
    row = data[0]["data"] if data else {}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(row, indent=1))
    return row


def doc_links(html: str, base: str) -> dict[str, str]:
    """Pick the known document kinds out of a bank page's links."""
    found: dict[str, str] = {}
    for href in re.findall(r'href="([^"]+)"', html):
        url = urljoin(base, href)
        for kind, pat in DOC_PATTERNS.items():
            if kind not in found and re.search(pat, url, re.I):
                found[kind] = url
    return found


ROW_RE = re.compile(
    r'views-field-title"><a href="(?P<href>[^"]+)"[^>]*>(?P<name>[^<]+)</a>.*?views-field-field-cert">\s*(?P<cert>\d+)',
    re.S)


def parse_list_page(html: str) -> dict[int, str]:
    """CERT → bank page URL from one page of the HTML failed-bank list."""
    return {int(m["cert"]): urljoin(FDIC_BASE, m["href"]) for m in ROW_RE.finditer(html)}


def crawl_bank_pages(s: requests.Session, max_pages: int = 60) -> dict[int, str]:
    """Walk the paginated list once → data/public/bank_pages.json (CERT → page URL)."""
    out = PUBLIC / "bank_pages.json"
    if out.exists():
        return {int(k): v for k, v in json.loads(out.read_text()).items()}
    pages: dict[int, str] = {}
    for n in range(max_pages):
        found = parse_list_page(_get(s, SOURCES["failed_bank_list_html"].format(page=n)).text)
        if not found or set(found) <= set(pages):
            break
        pages.update(found)
        time.sleep(0.5)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(pages, indent=1))
    return pages


def fetch_bank_docs(s: requests.Session, name: str, cert: int, page_url: str | None = None) -> dict[str, str]:
    """Download the PDFs linked from a failed bank's page → data/public/banks/<cert>/."""
    dest = PUBLIC / "banks" / str(cert)
    index = dest / "links.json"
    if index.exists():
        return {k: v for k, v in json.loads(index.read_text()).items() if k != "page"}
    html = page = None
    if page_url:
        try:
            page, html = page_url, _get(s, page_url).text
        except requests.HTTPError:
            html = None
    for slug in ([] if html else slug_candidates(name)):
        page = SOURCES["bank_page"].format(slug=slug)
        try:
            html = _get(s, page).text
            break
        except requests.HTTPError:
            continue
    if html is None:
        print(f"  no page for {name} (tried {slug_candidates(name)})", file=sys.stderr)
        return {}
    links = doc_links(html, page)
    dest.mkdir(parents=True, exist_ok=True)
    for kind, url in links.items():
        # Legacy links often have no .pdf extension, so check the bytes, not the URL.
        if kind in ("bid_summary", "faq"):
            continue
        body = _get(s, url).content
        if body.startswith(b"%PDF"):
            (dest / f"{kind}.pdf").write_bytes(body)
    index.write_text(json.dumps({"page": page, **links}, indent=1))
    return links


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--docs", type=int, default=5, help="fetch documents for the N most recent failures")
    ap.add_argument("--years", help="instead, fetch documents for failures in these years, e.g. 2008-2012")
    ap.add_argument("--limit", type=int, default=20, help="max banks when using --years")
    ap.add_argument("--sample", type=int, default=0, help="random sample with this seed (with --years)")
    a = ap.parse_args()
    s = _session()
    failures = json.loads(fetch_failures(s).read_text())
    print(f"failures: {len(failures)} rows")
    print(f"failed bank list: {fetch_failed_bank_list(s)}")
    names = load_failed_bank_list()
    pages = crawl_bank_pages(s)
    print(f"bank pages: {len(pages)}")
    if a.years:
        lo, _, hi = a.years.partition("-")
        hi = hi or lo
        rows = [r for r in failures if lo <= r["FAILYR"] <= hi and int(r["CERT"]) in pages]
        if a.sample:   # spread across the years instead of the most recent N
            import random
            rows = random.Random(a.sample).sample(rows, min(a.limit, len(rows)))
        rows = rows[: a.limit]
    else:
        rows = failures[: a.docs]
    for row in rows:
        cert = int(row["CERT"])
        name = names.get(cert, {}).get("Bank Name") or row["NAME"].title()
        links = fetch_bank_docs(s, name, cert, pages.get(cert))
        mix = fetch_loan_mix(s, cert)
        print(f"  {row['FAILDATE']:>10}  {name} (cert {cert}): docs={sorted(links)} "
              f"loans=${mix.get('LNLSNET', '?')}k as of {mix.get('REPDTE', '?')}")


if __name__ == "__main__":
    main()
