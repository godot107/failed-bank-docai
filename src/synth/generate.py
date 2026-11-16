"""Generate synthetic Tier A loan files (docs/SYNTHETIC_DATA.md) with exact labels.

For each fictional bank (stories.BANKS) and loan:
  samples/synthetic/pdf/<bank>/<loan_id>.pdf    one loan file = several documents concatenated (not tracked in git)
  samples/synthetic/labels/<loan_id>.json       document page ranges, canonical field values, planted flags + quotes
  samples/synthetic/data_room/<bank>.csv        the servicing system's loan schedule (reconciliation target)
  samples/synthetic/index.csv                   loan_id, bank, style, split, loan_type, pages, scanned docs

Everything is fictional and every page is watermarked SYNTHETIC. Layouts, label wording, date/money
formats and note-writing style vary by bank style; test banks use styles not seen in training.

Usage: .venv/bin/python src/synth/generate.py [--loans 25] [--seed 7] [--banks B1,B5]
"""
from __future__ import annotations

import argparse
import csv
import re
import io
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

import pypdfium2 as pdfium
from jinja2 import Environment
from markupsafe import Markup, escape
from PIL import Image, ImageFilter
from pypdf import PdfReader, PdfWriter
from weasyprint import HTML

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stories as st  # noqa: E402
import text as tx  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SYN = ROOT / "samples" / "synthetic"

# ---------------------------------------------------------------- bank styles
STYLES = {
    "S1": dict(font="Georgia, serif", accent="#1f3b5a", date="%m/%d/%Y", money="${:,.2f}", scan_p=0.3,
               labels=dict(principal="Principal Amount", rate="Interest Rate", maturity="Maturity Date",
                           payment="Monthly Payment", upb="Unpaid Principal Balance", next_due="Next Payment Due",
                           value="Appraised Value", note_date="Date of Note")),
    "S2": dict(font="Helvetica, Arial, sans-serif", accent="#5a1f1f", date="%B %d, %Y", money="${:,.2f}", scan_p=0.5,
               labels=dict(principal="Original Loan Amount", rate="Note Rate", maturity="Final Payment Date",
                           payment="Payment (P&I)", upb="Current Principal Balance", next_due="Due Date",
                           value="Market Value", note_date="Loan Date")),
    "S3": dict(font="Courier New, monospace", accent="#000000", date="%d-%b-%Y", money="{:,.2f}", scan_p=0.7,
               labels=dict(principal="AMT FINANCED", rate="RATE", maturity="MATURES", payment="PMT AMT",
                           upb="PRIN BAL", next_due="NEXT DUE", value="APPR VALUE", note_date="NOTE DT")),
    # final holdout style (B7): wording, formats and font appear nowhere else
    "S6": dict(font="Book Antiqua, Palatino Linotype, serif", accent="#3b1f5a", date="%d.%m.%Y", money="{:,.2f} $",
               scan_p=0.5, rate_fmt="{:.3f} p.a.",
               labels=dict(principal="Capital Advanced", rate="Yearly Charge", maturity="Repay By", payment="Amount Each Month",
                           upb="Owed Now", next_due="Following Due", value="Collateral Worth", note_date="Signed")),
    # second holdout (B8, 2026-11-11): slash day-first dates, new wording, and HARD packets (multi-page payment
    # histories, missing titles, two letters in a row) to test the classifier beyond one-page titled documents
    "S7": dict(font="Garamond, Georgia, serif", accent="#1f4a4a", date="%d/%m/%Y", money="${:,.2f}", scan_p=0.5,
               rate_fmt="{:.3f}%", hard=True,
               labels=dict(principal="Original Balance", rate="Coupon", maturity="Final Due Date", payment="Installment Amount",
                           upb="Remaining Balance", next_due="Next Installment", value="Valuation", note_date="Effective Date")),
    # third holdout (B9, 2026-11-11): scored ONCE with v3 + classifier v2 + current rules frozen
    "S8": dict(font="Cambria, Georgia, serif", accent="#5a3b1f", date="%d %b %Y", money="{:,.2f} dollars", scan_p=0.5,
               rate_fmt="{:.3f} % per annum", layout="two_col",
               labels=dict(principal="Note Amount", rate="Annual Rate", maturity="Loan Ends", payment="Periodic Payment",
                           upb="Balance Remaining", next_due="Upcoming Due", value="Valued At", note_date="Note Signed")),
    # test styles: never seen in training
    "S4": dict(font="Times New Roman, serif", accent="#2d4a1f", date="%Y-%m-%d", money="USD {:,.2f}", scan_p=0.4,
               labels=dict(principal="Sum Borrowed", rate="Annual Percentage of Interest", maturity="Term Ends",
                           payment="Installment", upb="Outstanding Principal", next_due="Installment Due On",
                           value="Opinion of Value", note_date="Executed On")),
    "S5": dict(font="Verdana, sans-serif", accent="#4a3a1f", date="%b %d %Y", money="${:,.0f}", scan_p=0.6,
               labels=dict(principal="Loan amt", rate="Rate %", maturity="Matures on", payment="Pmt",
                           upb="Bal", next_due="Due", value="Value", note_date="Dated")),
}

CSS = """
@page { size: Letter; margin: 0.8in; @bottom-center { content: "SYNTHETIC - FOR RESEARCH - fictional bank and people"; font-size: 7pt; color: #999; } }
body { font-family: {{ s.font }}; font-size: 10.5pt; color: #111; }
h1 { font-size: 15pt; color: {{ s.accent }}; border-bottom: 2px solid {{ s.accent }}; padding-bottom: 4px; }
h2 { font-size: 11.5pt; color: {{ s.accent }}; margin-top: 14px; }
table { border-collapse: collapse; width: 100%; margin-top: 6px; }
td, th { border: 1px solid #bbb; padding: 3px 6px; font-size: 9.5pt; text-align: left; }
.kv td:first-child { width: 42%; font-weight: bold; }
.bank { font-size: 9pt; color: #444; }
.wm { position: fixed; top: 45%; left: 10%; font-size: 48pt; color: rgba(200,0,0,0.07); transform: rotate(-30deg); }
"""

PAGE = """<html><head><style>{{ css }}</style></head><body><div class="wm">SYNTHETIC</div>
<div class="bank">{{ bank.name }} · {{ bank_city }}</div>{{ body }}</body></html>"""

DOCS = {
"promissory_note": """<h1>{{ 'PROMISSORY NOTE' if style != 'S5' else 'Note' }}</h1>
<p>{{ L.note_date }}: {{ f.note_date }} &nbsp;&nbsp; Loan No. {{ loan.loan_number }}</p>
<p>FOR VALUE RECEIVED, {{ loan.borrower_name }} ("Borrower") promises to pay to {{ bank.name }} ("Lender") the
principal sum stated below, with interest on the unpaid principal at the rate stated below, until paid in full.</p>
{{ note_kv }}
<p>{% if loan.interest_only %}Interest only is payable monthly; all principal is due at maturity.{% else %}Payments of
principal and interest are due monthly beginning one month after the date of this Note.{% endif %}
A late charge of 5% of any payment more than 15 days late will be assessed.</p>
<p style="margin-top:40px">______________________________ Borrower</p>""",

"security_instrument": """<h1>{{ 'DEED OF TRUST' if style in ('S1','S3','S5') else 'MORTGAGE' }}</h1>
<p>This Security Instrument is made on {{ f.note_date }}. The grantor is {{ loan.borrower_name }} ("Borrower").
The beneficiary is {{ bank.name }} ("Lender"). Borrower owes Lender the principal sum of {{ f.original_principal }}.</p>
<h2>Property</h2><p>{{ loan.property_address }}</p>
<p>This Security Instrument secures repayment of the debt evidenced by Borrower's note dated the same date, with the
full debt, if not paid earlier, due and payable on {{ f.maturity_date }}.</p>
<p>Recording reference: Instrument {{ rec_no }}, recorded {{ f.rec_date }}.</p>""",

"appraisal_1004": """<h1>{{ 'Uniform Residential Appraisal Report' if loan.loan_type == 'residential_1_4' else 'Commercial Appraisal Summary' }}</h1>
{{ appr_kv }}
<h2>Reconciliation</h2><p>Indicated value by sales comparison approach supports the opinion of market value stated above,
as of the effective date, subject to the assumptions and limiting conditions of this report.</p>""",

"loan_application_1003": """<h1>{{ 'Uniform Residential Loan Application' if style != 'S3' else 'LOAN APPLICATION' }}</h1>
<table class="kv">
<tr><td>Borrower</td><td>{{ loan.borrower_name }}</td></tr>
<tr><td>Amount Requested</td><td>{{ f.original_principal }}</td></tr>
<tr><td>Employer</td><td>{{ employer }}</td></tr>
<tr><td>Gross Annual Income (stated)</td><td>{{ f.stated_income }}</td></tr>
<tr><td>Purpose</td><td>{{ purpose }}</td></tr>
</table>
<p>I certify that the information provided in this application is true and correct as of the date set forth.</p>""",

"settlement_statement": """<h1>{{ 'SETTLEMENT STATEMENT' }}</h1><p class="bank">Settlement date {{ f.note_date }} · File {{ loan.loan_number }}</p>
<table><tr><th>Item</th><th>Amount</th></tr>
<tr><td>{{ L.principal }}</td><td>{{ f.original_principal }}</td></tr>
<tr><td>Loan origination charge</td><td>{{ f.orig_fee }}</td></tr>
<tr><td>Appraisal fee</td><td>{{ f.appraisal_fee }}</td></tr>
<tr><td>Title services</td><td>{{ f.title_fee }}</td></tr>
<tr><td>{{ L.rate }}</td><td>{{ f.note_rate_pct }}</td></tr>
<tr><td>{{ L.payment }}</td><td>{{ f.monthly_payment }}</td></tr>
</table>""",

"servicing_history": """<h1>{{ 'Loan History Statement' if style != 'S3' else 'ACCOUNT HISTORY' }}</h1>
{{ svc_kv }}
<h2>Recent Transactions</h2>
<table><tr><th>Date</th><th>Description</th><th>Amount</th></tr>
{% for t in txns %}<tr><td>{{ t[0] }}</td><td>{{ t[1] }}</td><td>{{ t[2] }}</td></tr>{% endfor %}
</table>""",

"servicer_comments": """<h1>{{ 'Servicing Comments' if style != 'S3' else 'COLLECTOR NOTES' }}</h1>
<p class="bank">Loan {{ loan.loan_number }} · {{ loan.borrower_name }}</p>
<table><tr><th style="width:18%">Date</th><th style="width:12%">User</th><th>Comment</th></tr>
{% for e in entries %}<tr><td>{{ e.date_fmt }}</td><td>{{ e.user }}</td><td>{{ e.text }}</td></tr>{% endfor %}
</table>""",

"borrower_letter": """<p>{{ f.letter_date }}</p><p>{{ letter.opening }}</p><p>{{ letter.text }}</p>
<p>Loan number: {{ loan.loan_number }}</p><p>Sincerely,<br/>{{ loan.borrower_name }}</p>""",

"credit_memo": """<h1>{{ 'Credit Memorandum' if style != 'S3' else 'CREDIT APPROVAL' }}</h1>
<table class="kv">
<tr><td>Borrower</td><td>{{ loan.borrower_name }}</td></tr>
<tr><td>Request</td><td>{{ f.original_principal }} {{ loan.loan_type.replace('_', ' ') }}</td></tr>
<tr><td>{{ L.rate }}</td><td>{{ f.note_rate_pct }}</td></tr>
{% if loan.dti_stated_pct %}<tr><td>Debt-to-Income (as underwritten)</td><td>{{ loan.dti_stated_pct }}%</td></tr>{% endif %}
<tr><td>Risk Rating</td><td>{{ loan.risk_rating.replace('_', ' ').title() }}</td></tr>
</table>
<h2>Analysis</h2><p>{{ analysis }}</p>
{% for m in memo %}<p>{{ m.text }}</p>{% endfor %}
<h2>Recommendation</h2><p>{{ 'Approve as presented.' if style != 'S3' else 'APPROVE.' }}</p>""",
}

ENV = Environment(autoescape=True)
FAKE_CITIES = ["Riverton", "Oak Hollow", "Millbrook", "Cedar Falls", "Westbury", "Granite Bay"]


def fmt_date(d: str | date, s: dict) -> str:
    d = date.fromisoformat(d) if isinstance(d, str) else d
    return d.strftime(s["date"])


def fmt_money(x: float | None, s: dict) -> str:
    return "" if x is None else s["money"].format(x)


def fmt_rate(x: float, style: str) -> str:
    if style in STYLES and "rate_fmt" in STYLES[style]:
        return STYLES[style]["rate_fmt"].format(x)
    return {"S3": f"{x:.3f}", "S5": f"{x:g}%"}.get(style, f"{x:.3f}%")


# ---------------------------------------------------------------- augmentation (training banks only)
# Pools exclude every validation (S3) and test (S4, S5) label, date format, money format and font, so augmented
# training data can't leak the held-out banks' wording into the model.
AUG_LABELS = {
    "principal": ["Principal Amount", "Original Loan Amount", "Loan Principal", "Face Amount", "Original Principal",
                  "Amount of Note", "Principal Sum"],
    "rate": ["Interest Rate", "Note Rate", "Rate of Interest", "Contract Rate", "Annual Interest Rate"],
    "maturity": ["Maturity Date", "Final Payment Date", "Due in Full On", "Final Maturity", "Maturity"],
    "payment": ["Monthly Payment", "Payment (P&I)", "Scheduled Payment", "Regular Payment", "P&I Payment"],
    "upb": ["Unpaid Principal Balance", "Current Principal Balance", "Principal Balance", "UPB", "Remaining Principal"],
    "next_due": ["Next Payment Due", "Due Date", "Next Due Date", "Payment Due", "Next Scheduled Payment"],
    "value": ["Appraised Value", "Market Value", "As-Is Value", "Estimated Value", "Value Conclusion"],
    "note_date": ["Date of Note", "Loan Date", "Note Date", "Origination Date", "Closing Date"],
}
AUG_FONTS = ["Georgia, serif", "Helvetica, Arial, sans-serif", "Palatino, serif", "Trebuchet MS, sans-serif",
             "Liberation Sans Narrow, sans-serif", "DejaVu Serif, serif", "Courier, monospace"]
AUG_DATES = ["%m/%d/%Y", "%B %d, %Y", "%m-%d-%Y", "%d %B %Y", "%m/%d/%y", "%A, %B %d, %Y"]
AUG_MONEY = ["${:,.2f}", "$ {:,.2f}", "${:,.2f} USD"]
AUG_RATES = ["{:.3f}%", "{:.2f}%", "{:.3f} percent", "{:.4f}%"]
LAYOUTS = ["table", "inline", "stacked", "two_col"]


def random_style(rng: random.Random) -> dict:
    return dict(font=rng.choice(AUG_FONTS), accent=f"#{rng.randint(0, 0x555555):06x}", date=rng.choice(AUG_DATES),
                money=rng.choice(AUG_MONEY), rate_fmt=rng.choice(AUG_RATES), scan_p=rng.uniform(0.2, 0.8),
                harsh=rng.random() < 0.5, layout=rng.choice(LAYOUTS), shuffle=True,
                distractors=rng.random() < 0.7, omit_p=0.1,
                labels={k: rng.choice(v) for k, v in AUG_LABELS.items()})


def kv_html(rows: list[tuple[str, str]], layout: str) -> Markup:
    e = lambda x: escape(str(x))  # noqa: E731
    if layout == "inline":
        return Markup("".join(f"<p>{e(k)}: {e(v)}</p>" for k, v in rows))
    if layout == "stacked":
        return Markup("".join(f'<div style="margin:6px 0"><div style="font-size:8pt;color:#555">{e(k)}</div>'
                              f'<div style="font-size:11pt">{e(v)}</div></div>' for k, v in rows))
    if layout == "two_col":
        cells = [f"<td><b>{e(k)}</b></td><td>{e(v)}</td>" for k, v in rows]
        trs = ["<tr>" + "".join(cells[i:i + 2]) + "</tr>" for i in range(0, len(cells), 2)]
        return Markup('<table>' + "".join(trs) + "</table>")
    return Markup('<table class="kv">' + "".join(f"<tr><td>{e(k)}</td><td>{e(v)}</td></tr>" for k, v in rows) + "</table>")


def doc_types_for(loan: st.Loan, letter, hard: bool = False) -> list[str]:
    types = ["promissory_note"]
    if loan.loan_type in st.SECURED:
        types += ["security_instrument", "appraisal_1004"]
    if loan.loan_type in ("residential_1_4", "consumer"):
        types += ["loan_application_1003"]
    if loan.loan_type == "residential_1_4":
        types += ["settlement_statement"]
    types += ["credit_memo", "servicing_history", "servicer_comments"]
    if letter:
        types += ["borrower_letter"]
        if hard:
            types += ["borrower_letter"]        # a follow-up letter: two documents of the same type in a row
    return types


def render_doc(doc_type: str, ctx: dict) -> bytes:
    body = ENV.from_string(DOCS[doc_type]).render(**ctx)          # data is escaped here
    if ctx.get("notitle"):                                          # hard packets: some documents lack a title
        body = re.sub(r"<h1>.*?</h1>", "", body, count=1, flags=re.S)
    # the page shell inserts the already-built CSS and body markup, so it renders without escaping
    html = Environment(autoescape=False).from_string(PAGE).render(
        css=Environment().from_string(CSS).render(s=ctx["s"]), body=body, **ctx)
    return HTML(string=html).write_pdf()


def scan(pdf: bytes, rng: random.Random, harsh: bool = False) -> bytes:
    """Rasterize like a scanned/faxed page: low dpi, slight skew, blur, JPEG. harsh = fax-style black/white,
    lower resolution, more skew, and sometimes a rubber stamp (augmented training banks only)."""
    doc = pdfium.PdfDocument(pdf)
    imgs = []
    for page in doc:
        img = page.render(scale=rng.uniform(1.0, 1.3) if harsh else rng.uniform(1.4, 1.9)).to_pil().convert("L")
        if harsh and rng.random() < 0.4:
            from PIL import ImageDraw
            d = ImageDraw.Draw(img)
            x, y = rng.randint(50, img.width - 250), rng.randint(50, img.height - 120)
            d.rectangle([x, y, x + 180, y + 60], outline=90, width=4)
            d.text((x + 20, y + 20), rng.choice(["PAID", "COPY", "RECEIVED", "FILE"]), fill=90)
        img = img.rotate(rng.uniform(-3, 3) if harsh else rng.uniform(-1.5, 1.5), expand=True, fillcolor=255)
        if harsh and rng.random() < 0.5:
            img = img.point(lambda v: 255 if v > rng.randint(120, 170) else 0)
        if rng.random() < 0.5:
            img = img.filter(ImageFilter.GaussianBlur(rng.uniform(0.3, 0.8)))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=rng.randint(45, 75))
        imgs.append(Image.open(io.BytesIO(buf.getvalue())).convert("RGB"))
    out = io.BytesIO()
    imgs[0].save(out, "PDF", save_all=True, append_images=imgs[1:], resolution=150)
    return out.getvalue()


def build_loan_file(loan: st.Loan, bank: dict, rng: random.Random) -> tuple[bytes, dict]:
    style = bank["style"]
    s = STYLES[style]
    note_d = date.fromisoformat(loan.note_date)
    comment_dates = sorted({(st.CLOSING_DATE - timedelta(days=rng.randint(5, 900))).isoformat() for _ in range(14)})
    tstyle = bank.get("text_style", style)        # augmented banks borrow a TRAINING note-writing style
    entries = tx.comment_log(rng, tstyle, loan.flags, loan.decoys, comment_dates)
    for e in entries:
        e["date_fmt"] = fmt_date(e["date"], s)
        e["user"] = rng.choice(["JMR", "KTL", "svc04", "AB2", "dlh"])
    memo = tx.memo_sentences(rng, tstyle, loan.flags)
    letter = tx.borrower_letter(rng, tstyle, loan.flags, loan.loan_number)

    f = {
        "note_date": fmt_date(note_d, s), "original_principal": fmt_money(loan.original_principal, s),
        "note_rate_pct": fmt_rate(loan.note_rate_pct, style), "maturity_date": fmt_date(loan.maturity_date, s),
        "monthly_payment": fmt_money(loan.monthly_payment, s), "current_upb": fmt_money(loan.current_upb, s),
        "next_due_date": fmt_date(loan.next_due_date, s), "appraised_value": fmt_money(loan.appraised_value, s),
        "stated_income": fmt_money(loan.stated_income, s), "as_of": fmt_date(st.CLOSING_DATE - timedelta(days=3), s),
        "appraisal_date": fmt_date(note_d - timedelta(days=rng.randint(10, 40)), s),
        "rec_date": fmt_date(note_d + timedelta(days=rng.randint(2, 12)), s),
        "letter_date": fmt_date(st.CLOSING_DATE - timedelta(days=rng.randint(20, 200)), s),
        "orig_fee": fmt_money(round(loan.original_principal * 0.01, 2), s),
        "appraisal_fee": fmt_money(rng.choice([350, 400, 450, 525]), s), "title_fee": fmt_money(rng.randint(600, 1800), s),
    }
    L = s["labels"]
    omit_p = s.get("omit_p", 0.0)
    printed = {"promissory_note": {"note_date"}, "servicing_history": set(), "appraisal_1004": set()}

    def target(doc, field, label, value):
        if omit_p and rng.random() < omit_p:     # base banks never call rng here (keeps them reproducible)
            return []
        printed[doc].add(field)
        return [(label, value)]

    rows = (target("promissory_note", "original_principal", L["principal"], f["original_principal"])
            + target("promissory_note", "note_rate_pct", L["rate"], f["note_rate_pct"])
            + target("promissory_note", "monthly_payment", L["payment"], f["monthly_payment"])
            + target("promissory_note", "maturity_date", L["maturity"], f["maturity_date"]))
    svc = ([("Loan Number", loan.loan_number), ("Statement As Of", f["as_of"])]
           + target("servicing_history", "current_upb", L["upb"], f["current_upb"])
           + target("servicing_history", "next_due_date", L["next_due"], f["next_due_date"])
           + [("Days Past Due", loan.days_delinquent)])
    appr = ([("Property Address", loan.property_address), ("Borrower", loan.borrower_name), ("Lender/Client", bank["name"]),
             ("Property Type", "{prop_type}"), ("Effective Date of Appraisal", f["appraisal_date"])]
            + target("appraisal_1004", "appraised_value", L["value"], f["appraised_value"]))
    if s.get("distractors"):
        rows += [("Late Charge", fmt_rate(5.0, style)), ("Prepayment Penalty", fmt_money(loan.original_principal * 0.02, s))]
        svc += [("Escrow Balance", fmt_money(rng.uniform(200, 4000), s)), ("Late Charges Due", fmt_money(loan.monthly_payment * 0.05, s)),
                ("Prior Month Balance", fmt_money(loan.current_upb + loan.monthly_payment * 0.3, s))]
        if loan.appraised_value:
            appr += [("Land Value", fmt_money(loan.appraised_value * 0.25, s)), ("Contract Price", fmt_money(loan.appraised_value * 0.98, s))]
    if s.get("shuffle"):
        rng.shuffle(rows); rng.shuffle(svc); rng.shuffle(appr)
    elif style in ("S2", "S4"):
        rows.reverse()
    txns = []
    for k in range(40 if s.get("hard") else 6):      # hard packets: a 2-page payment history
        d = date.fromisoformat(loan.next_due_date) - timedelta(days=30 * (k + 1))
        txns.append((fmt_date(d, s), "Payment" if k >= loan.days_delinquent // 30 else "Late charge assessed",
                     fmt_money(loan.monthly_payment if k >= loan.days_delinquent // 30 else loan.monthly_payment * 0.05, s)))
    layout = s.get("layout", "table")
    bank_city = rng.choice(FAKE_CITIES)                       # same rng order as before augmentation existed
    rec_no = f"{note_d.year}-{rng.randint(100000, 999999)}"
    prop_type = {"residential_1_4": "Single Family", "cre": rng.choice(["Retail Strip Center", "Office", "Warehouse"]),
                 "construction": "Proposed Construction"}.get(loan.loan_type, "")
    appr = [(k, prop_type if v == "{prop_type}" else v) for k, v in appr]
    ctx = dict(loan=loan, bank=bank, bank_city=bank_city, s=s, style=style, L=L, f=f, rows=rows,
               note_kv=kv_html(rows, layout), svc_kv=kv_html(svc, layout), appr_kv=kv_html(appr, layout),
               txns=txns, entries=entries, memo=memo, letter=letter,
               rec_no=rec_no,
               prop_type=prop_type,
               employer=rng.choice(["Self-employed", "Regional Hospital", "County Schools", "Builders Supply Co."]),
               purpose=rng.choice(["Purchase", "Cash-out refinance", "Rate/term refinance"]),
               analysis=rng.choice(["Primary repayment from borrower cash flow; secondary source is the collateral.",
                                    "Repayment from sale of completed units; guarantor support available.",
                                    "Cash flow coverage is adequate based on projections provided by the borrower."]))

    writer, docs, scanned, page = PdfWriter(), [], [], 1
    for dt in doc_types_for(loan, letter, s.get("hard", False)):
        if s.get("hard"):
            ctx["notitle"] = rng.random() < 0.4
        pdf = render_doc(dt, ctx)
        if rng.random() < s["scan_p"]:
            pdf, was_scanned = scan(pdf, rng, harsh=s.get("harsh", False)), True
        else:
            was_scanned = False
        reader = PdfReader(io.BytesIO(pdf))
        n = len(reader.pages)
        for p in reader.pages:
            writer.add_page(p)
        docs.append({"doc_type": dt, "page_from": page, "page_to": page + n - 1, "scanned": was_scanned,
                     "printed": sorted(printed.get(dt, []))})
        scanned.append(was_scanned)
        page += n
    out = io.BytesIO()
    writer.write(out)

    printed = set()
    for d in docs:
        printed.add(d["doc_type"])
    fields = {
        "loan_number": loan.loan_number, "borrower_name": loan.borrower_name, "property_address": loan.property_address,
        "loan_type": loan.loan_type, "note_date": loan.note_date, "original_principal": loan.original_principal,
        "note_rate_pct": loan.note_rate_pct, "maturity_date": loan.maturity_date, "monthly_payment": loan.monthly_payment,
        "appraised_value": loan.appraised_value, "current_upb": loan.current_upb, "next_due_date": loan.next_due_date,
        "days_delinquent": loan.days_delinquent,
        "ltv_pct": round(100 * loan.current_upb / loan.appraised_value, 2) if loan.appraised_value else None,
        "stated_income": loan.stated_income,
    }
    flags = [{"flag": e["flag"], "doc_type": "servicer_comments", "quote": e["text"]} for e in entries if e["flag"]]
    flags += [{"flag": m["flag"], "doc_type": "credit_memo", "quote": m["text"]} for m in memo]
    if letter:
        flags.append({"flag": letter["flag"], "doc_type": "borrower_letter", "quote": letter["text"]})
    decoys = [{"topic": e["decoy"], "doc_type": "servicer_comments", "quote": e["text"]} for e in entries if e["decoy"]]
    label = {
        "loan_id": loan.loan_id, "bank_id": bank["bank_id"], "style": style, "split": bank["split"],
        "documents": docs, "fields": fields, "flags": flags, "decoys": decoys,
        "story_flags": loan.flags, "risk_rating": loan.risk_rating,
        "cross_checks": {"income_mismatch": "income_mismatch" in loan.flags, "verified_income": loan.verified_income},
        "data_room": loan.data_room, "data_room_mismatch": loan.data_room_mismatch,
    }
    return out.getvalue(), label


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--loans", type=int, default=25, help="loans per bank")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--banks", help="comma-separated bank ids (default: all)")
    ap.add_argument("--augment", type=int, default=0, help="add N randomized TRAINING banks (split train_aug)")
    ap.add_argument("--aug-loans", type=int, default=12, help="loans per augmented bank")
    ap.add_argument("--aug-hard", action="store_true",
                    help="augmented banks with HARD packets (multi-page payment histories, missing titles, back-to-back "
                         "letters) for classifier training (bank ids H01.., split train_hard)")
    ap.add_argument("--aug-plain", action="store_true",
                    help="augmented banks WITHOUT field omission and distractor amounts (bank ids P01.., split train_plain)")
    a = ap.parse_args()
    banks = [b for b in st.BANKS if not a.banks or b["bank_id"] in a.banks.split(",")]
    aug_rng = random.Random(f"{a.seed}-augment" + ("-hard" if a.aug_hard else ""))
    train_certs = [b["model_cert"] for b in st.BANKS if b["split"] == "train"]
    for k in range(1, a.augment + 1):
        sid = f"{'H' if a.aug_hard else ('P' if a.aug_plain else 'A')}{k:02d}"
        STYLES[sid] = random_style(aug_rng)        # same draws as A01..: P banks differ ONLY by the two switches below
        if a.aug_plain or a.aug_hard:
            STYLES[sid]["omit_p"] = 0.0
            STYLES[sid]["distractors"] = False
        if a.aug_hard:
            STYLES[sid]["hard"] = True
        banks.append({"bank_id": sid, "name": f"{aug_rng.choice(FAKE_CITIES)} {aug_rng.choice(['Savings Bank', 'State Bank', 'Bank & Trust', 'Community Bank', 'National Bank'])}",
                      "model_cert": aug_rng.choice(train_certs), "style": sid,
                      "split": "train_hard" if a.aug_hard else ("train_plain" if a.aug_plain else "train_aug"),
                      "text_style": aug_rng.choice(["S1", "S2"]), "n_loans": a.aug_loans})
    (SYN / "labels").mkdir(parents=True, exist_ok=True)
    (SYN / "data_room").mkdir(parents=True, exist_ok=True)
    index = []
    for bank in banks:
        rng = random.Random(f"{a.seed}-render-{bank['bank_id']}")
        loans = st.make_bank_loans(bank, bank.get("n_loans", a.loans), a.seed)
        pdf_dir = SYN / "pdf" / bank["bank_id"]
        pdf_dir.mkdir(parents=True, exist_ok=True)
        with open(SYN / "data_room" / f"{bank['bank_id']}.csv", "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["loan_number", "loan_type", "original_principal", "note_rate_pct", "maturity_date", "current_upb"])
            for loan in loans:
                dr = loan.data_room
                w.writerow([loan.loan_number, loan.loan_type, dr["original_principal"], dr["note_rate_pct"],
                            dr["maturity_date"], dr["current_upb"]])
        for loan in loans:
            pdf, label = build_loan_file(loan, bank, rng)
            (pdf_dir / f"{loan.loan_id}.pdf").write_bytes(pdf)
            (SYN / "labels" / f"{loan.loan_id}.json").write_text(json.dumps(label, indent=1))
            index.append({"loan_id": loan.loan_id, "bank_id": bank["bank_id"], "style": bank["style"],
                          "split": bank["split"], "loan_type": loan.loan_type,
                          "pages": label["documents"][-1]["page_to"],
                          "docs": len(label["documents"]), "scanned_docs": sum(d["scanned"] for d in label["documents"]),
                          "flags": ";".join(sorted({f["flag"] for f in label["flags"]}))})
        print(f"{bank['bank_id']} {bank['name']} ({bank['split']}, {bank['style']}): {len(loans)} loan files")
    old = SYN / "index.csv"
    if old.exists():
        done = {b["bank_id"] for b in banks}
        index = sorted([r for r in csv.DictReader(open(old)) if r["bank_id"] not in done] + index, key=lambda r: r["loan_id"])
    with open(SYN / "index.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(index[0]))
        w.writeheader()
        w.writerows(index)
    pages = sum(int(r["pages"]) for r in index)
    print(f"{len(index)} loan files, {pages} pages → {SYN}")
    print(f"DI cost if all pages ran on S0: prebuilt/Layout ${pages * 10 / 1000:.2f}, custom ${pages * 30 / 1000:.2f}, "
          f"classifier ${pages * 3 / 1000:.2f}")


if __name__ == "__main__":
    main()
