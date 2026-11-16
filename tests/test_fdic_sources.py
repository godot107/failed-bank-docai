import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import yaml  # noqa: E402

import fdic_sources as fs  # noqa: E402


def test_slugify_basic():
    assert fs.slugify("Nano Banc") == "nano-banc"
    assert fs.slugify("Community Bank and Trust - West Georgia") == "community-bank-and-trust-west-georgia"


def test_slug_candidates_handle_ampersand_and_the():
    c = fs.slug_candidates("The Metro Bank & Trust")
    assert "the-metro-bank-and-trust" in c
    assert "metro-bank-trust" in c


def test_doc_links_picks_known_kinds():
    html = '''
      <a href="/bank-failures/purchase-assumption-agreement-x.pdf">P&A</a>
      <a href="/bank-failures/publication-notice-creditors-and-depositors-x.pdf">Notice</a>
      <a href="/bank-failures/bid-summary-x">Bids</a>
      <a href="/news/other">Other</a>'''
    links = fs.doc_links(html, "https://www.fdic.gov/bank-failures/failed-bank-list/x")
    assert links["pa_agreement"].endswith("purchase-assumption-agreement-x.pdf")
    assert links["pa_agreement"].startswith("https://www.fdic.gov/")
    assert "creditor_notice" in links and "bid_summary" in links
    assert len(links) == 3


def test_schema_sources_are_known_doc_types():
    schema = yaml.safe_load((fs.ROOT / "schema" / "loan_fields.yaml").read_text())
    types = set(schema["document_types"])
    for name, f in schema["fields"].items():
        for src in f.get("source", []):
            assert src in types, f"{name}: unknown source {src}"
    for k in schema["reconcile"]["compare"]:
        assert k in schema["fields"]


def test_parse_list_page_handles_new_and_legacy_links():
    row = ('<td headers="view-title-table-column" class="views-field views-field-title">'
           '<a href="{href}" hreflang="en">{name}</a></td>'
           '<td class="views-field views-field-field-cert">{cert}          </td>')
    html = (row.format(href="/resources/resolutions/bank-failures/failed-bank-list/ocala.html",
                       name="Ocala National Bank", cert=26538)
            + row.format(href="/bank-failures/failed-bank-list/nano-banc", name="Nano Banc", cert=58590))
    pages = fs.parse_list_page(html)
    assert pages[26538] == "https://www.fdic.gov/resources/resolutions/bank-failures/failed-bank-list/ocala.html"
    assert pages[58590] == "https://www.fdic.gov/bank-failures/failed-bank-list/nano-banc"


def test_legacy_pa_link_patterns():
    html = '<a href="/foia/files/washington_mutual_p_and_a.pdf">P&A</a>'
    assert "pa_agreement" in fs.doc_links(html, "https://www.fdic.gov/x")


def test_chunker_selects_chunks_and_remaps(tmp_path):
    from pypdf import PdfWriter
    import di_chunk as dc
    w = PdfWriter()
    for _ in range(7):
        w.add_blank_page(width=200, height=200)
    pdf = tmp_path / "x.pdf"
    with open(pdf, "wb") as f:
        w.write(f)
    texts = ["cover", "parties", "", "deposit premium 1%", "", "", "asset discount"]
    pages = dc.select_pages(texts, [r"deposit premium|asset discount"], context=0, always_first=1)
    assert pages == [1, 4, 7]
    chunks = dc.make_chunks(pdf, pages, size=2)
    assert [c.pages for c in chunks] == [[1], [4], [7]]          # non-contiguous pages don't share a chunk
    chunks = dc.make_chunks(pdf, [2, 3, 4, 5, 6], size=2)
    assert [c.pages for c in chunks] == [[2, 3], [4, 5], [6]]
    assert dc.remap([{"pageNumber": 2}], chunks[1]) == [{"pageNumber": 5}]
