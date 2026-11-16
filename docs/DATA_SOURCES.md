# Data sources

Everything here is public. All checked 2026-11-10; code in `src/fdic_sources.py` (others to be added).

## 1. Which FDIC data is about bank failures?

Most of fdic.gov/resources/data-tools is **not** about failures. It's built for **supervision** (examining
healthy banks) and for the public ("is my bank insured?"). The failure and receivership material is a
small part:

| FDIC function | What it does | Share of the data-tools page |
|---|---|---|
| Supervision | Examines healthy banks | **Most of it**: Call Reports, peer groups, deposit market share |
| **Resolution and receivership** | Sells failing banks, runs receiverships, sells leftover assets, pays claims | **One link** ("Failures and Assistance") + the Resolutions pages |
| Deposit insurance | Insures deposits, pays depositors | Small |

| Source | Use here |
|---|---|
| **Failures API** (`api.fdic.gov/banks/failures`): 4,117 rows since 1934 (3,524 failures + 593 assistance transactions); date, resolution type, buyer, assets, deposits, estimated cost to the insurance fund | The failure universe; the 2008–2012 wave (465 failures) |
| **Call Reports** (`api.fdic.gov/banks/financials?filters=CERT:<cert>`) | Each failed bank's last balance sheet: loan mix (1–4 family, construction, CRE, C&I, consumer). Sizes the synthetic portfolios; research question 1 |
| **Failed Bank List** (CSV + paginated HTML, since 2000) | Full names, buyer, closing date; the HTML list links each bank's page (578 pages) |
| **Each failed bank's page** | **Purchase & Assumption (P&A) agreement PDF**, creditor notice PDF, bid summary, FAQ. Recent banks: `/bank-failures/failed-bank-list/<slug>`. 2000–2015 banks: legacy `/resources/resolutions/bank-failures/failed-bank-list/<short>.html` (found by crawling the list) |
| **Resolutions Handbook** (`/bank-failures/resolutions-handbook.pdf`, 32 pages) | Process + glossary. Read with Document Intelligence Layout and split by section into 93 passages: the Loan Desk Q&A agent cites it by page (`src/knowledge.py`) |
| Historical asset sales (closed loan sales, real estate, since 2009–10) | Context for loan sales; check whether loan-level later |

## 2. Public unstructured documents (real text and PDFs)

| Source | What it is | Volume / form | Use | Verified |
|---|---|---|---|---|
| **P&A agreements, 2008–2012** | Contracts selling each failed bank to a healthy one; crisis-era ones carry **loss-share** exhibits ("Whole Bank w/ Loss Share") | Scanned/OCR'd PDFs, **65–169 pages** each (sampled: Bank of Miami 169, Chestatee State Bank 145, Community National Bank 65) | Document Intelligence Layout + clause extraction (buyer, date, asset scope, loss-share terms) | Yes: 10 downloaded (6 crisis-era, 4 recent) |
| **Inspector General material loss reviews (MLRs)** | Post-mortems on why costly failures happened (FDIC OIG; Treasury OIG for OCC/OTS banks; Federal Reserve OIG) | ~100+ PDFs, 2009–2012, 20–60 pages of narrative | Text analysis: failure causes (construction concentration, brokered deposits, weak management, appraisal problems) | FDIC OIG PDF returns 200 |
| **CFPB consumer complaints, mortgage product** | Consumer complaints with optional free-text narratives (narratives published from 2015; complaints from Dec 2011) | 466K mortgage complaints via API / bulk CSV | **Real text** for borrower-side risk flags (hardship, foreclosure, servicing errors). Drop company names in anything published | API returns hits |
| CFPB / GSE blank forms | Blank Closing Disclosure, Form 1003, Form 1004 | PDFs | Templates for realistic synthetic documents | To check |
| Federal Reserve OIG cross-cutting review (2011) | Summary of failed-bank review findings | PDF | Cross-check for the MLR text analysis | Link found |

**Structured, optional (registration required):** Fannie Mae Single-Family Loan Performance and Freddie Mac
Single-Family Loan-Level datasets. These are real loan-level data, including 2005–2008 vintages, and could
make the synthetic loans realistic (rates, LTVs, delinquency paths). Free, but they need an account (owner's decision).

## 3. What is NOT public (so it's synthetic here)

A failed bank's **loan files** (notes, deeds of trust, appraisals, payment histories, servicer notes) are
confidential borrower records, shown to bidders only in a secure data room. This project generates them
synthetically, with planted correct answers, sized and mixed to match a real failed bank's last Call Report.

## Gotchas
- The API abbreviates names ("FS&LA"); use the Failed Bank List CSV for full names.
- The CSV is cp1252 with non-breaking spaces in the header.
- Recent page slugs are inconsistent about "&" and "The"; legacy pages use short names (`ocala.html`), so the
  fetcher crawls the paginated list instead of guessing.
- Legacy P&A links often have no `.pdf` extension; check the bytes (`%PDF`).
- P&A PDFs are scans with OCR typos ("V ersion", "8OCC"). Good for testing, but expect noise.
