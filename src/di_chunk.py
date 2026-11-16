"""Fit long PDFs to Document Intelligence limits: pick the pages worth analyzing, split into chunks,
and stitch the per-chunk results back onto the original page numbers.

Why: DI's free tier (F0) analyzes only the first 2 pages of each request and 500 pages a month. A
2008-era P&A agreement runs 60-170 pages. Chunking makes every page reachable on F0. Page selection
(using the PDF's existing OCR text layer, which costs nothing) keeps the page count, and the cost on
S0, down to the pages that matter.

    python src/di_chunk.py data/public/banks/34578/pa_agreement.pdf --find "loss share" --chunk 2
"""
from __future__ import annotations

import argparse
import io
import re
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader, PdfWriter

F0_PAGES_PER_REQUEST = 2
F0_PAGES_PER_MONTH = 500


@dataclass
class Chunk:
    pages: list[int]          # 1-based page numbers in the ORIGINAL document
    pdf: bytes                # the chunk as its own PDF


def page_texts(path: Path) -> list[str]:
    """Existing text layer per page (free; scanned P&As were OCR'd at publication)."""
    return [(p.extract_text() or "") for p in PdfReader(str(path)).pages]


def select_pages(texts: list[str], patterns: list[str], context: int = 1, always_first: int = 2) -> list[int]:
    """1-based pages that match any pattern (case-insensitive), plus `context` pages either side,
    plus the first `always_first` pages (parties, date, recitals)."""
    hits = set(range(1, min(always_first, len(texts)) + 1))
    rx = [re.compile(p, re.I) for p in patterns]
    for i, t in enumerate(texts, start=1):
        if any(r.search(t) for r in rx):
            hits.update(range(max(1, i - context), min(len(texts), i + context) + 1))
    return sorted(hits)


def make_chunks(path: Path, pages: list[int], size: int = F0_PAGES_PER_REQUEST) -> list[Chunk]:
    """Group selected pages into chunks of `size` (contiguous runs stay together where possible)."""
    reader = PdfReader(str(path))
    chunks, current = [], []
    for p in pages:
        if current and (len(current) == size or p != current[-1] + 1):
            chunks.append(current)
            current = []
        current.append(p)
    if current:
        chunks.append(current)
    out = []
    for group in chunks:
        w = PdfWriter()
        for p in group:
            w.add_page(reader.pages[p - 1])
        buf = io.BytesIO()
        w.write(buf)
        out.append(Chunk(pages=group, pdf=buf.getvalue()))
    return out


def remap(result_pages: list[dict], chunk: Chunk) -> list[dict]:
    """DI numbers pages 1..n inside each chunk; map them back to the original page numbers."""
    fixed = []
    for page in result_pages:
        page = dict(page)
        page["pageNumber"] = chunk.pages[page["pageNumber"] - 1]
        fixed.append(page)
    return fixed


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--find", action="append", default=[], help="regex for pages to keep (repeatable)")
    ap.add_argument("--chunk", type=int, default=F0_PAGES_PER_REQUEST)
    a = ap.parse_args()
    texts = page_texts(a.pdf)
    pages = select_pages(texts, a.find) if a.find else list(range(1, len(texts) + 1))
    chunks = make_chunks(a.pdf, pages, a.chunk)
    print(f"{a.pdf.name}: {len(texts)} pages → {len(pages)} selected → {len(chunks)} chunks of ≤{a.chunk}")
    print(f"  F0: {len(pages)}/{F0_PAGES_PER_MONTH} free pages this month")
    print(f"  S0: Layout ${len(pages) * 10 / 1000:.2f} · Read ${len(pages) * 1.5 / 1000:.3f} "
          f"(eastus list prices per 1K pages: Layout $10, Read $1.50, custom $30, classifier $3; checked 2026-11-10)")


if __name__ == "__main__":
    main()
