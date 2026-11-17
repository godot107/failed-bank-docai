# Results

All numbers are on **synthetic** loan files (docs/SYNTHETIC_DATA.md, seed 7): 6 fictional banks, 150 loan
files, 978 pages. Training banks B1–B3 (styles S1, S2); validation bank B4 (style S3); test banks B5, B6
(styles S4, S5). Validation and test styles never appear in training. Thresholds are chosen on validation
and reported once on test.

## Phase 2: extraction (U1)

Fields: note date, original principal, note rate, monthly payment, maturity date (promissory note); current
unpaid balance, next due date (payment history); appraised value (appraisal).

### Rules baseline (Layout + label wording from the training banks)

| Banks | Fields | Recall | Precision | Wrong | Missing |
|---|---:|---:|---:|---:|---:|
| Training (B1–B3) | 587 | 83.8% | 100.0% | 0 | 95 |
| Validation (B4, unseen style) | 197 | **0.0%** | — | 0 | 197 |
| Test (B5–B6, unseen styles) | 388 | **0.0%** | — | 0 | 388 |

A parser written from one set of banks' wording finds nothing at a bank that writes "Sum Borrowed" instead of
"Principal Amount". It fails safely (missing, not wrong), but that means a person types every value.

### Custom neural model v1 (trained on the 3 training banks only)

| Banks | Fields | Recall | Precision | Wrong | Missing |
|---|---:|---:|---:|---:|---:|
| Validation (B4, style S3) | 197 | 99.5% | 100.0% | 0 | 1 |
| Test (B5–B6, styles S4, S5) | 388 | **86.9%** | **98.8%** | 4 | 47 |

All test misses come from **one** unseen bank (S4): fields in reversed order with unfamiliar labels ("Term Ends",
"Installment", "Sum Borrowed"). Maturity date 0/25, monthly payment 3/25, and 3 times the payment amount was read
as the principal (all three at confidence ≈ 0.32). The other test bank (S5) is ≈ 99%, with one OCR misread
("$1,811,DDD").

### Review routing (what a person sees, what slips through)

| Policy (cutoff chosen on validation, target ≤ 2% silent) | Test review load | Test silent errors |
|---|---:|---:|
| Confidence + validity rules | 12.1% | 4 / 388 (1.0%) |
| + reconciliation against the data-room schedule | 14.4% | 1 / 388 (0.3%) |
| + loan-to-value plausibility rule (30–160%) | 14.7% | **0 / 388** |
| New-bank burn-in (review everything) | 100% | 0 |

**Two honest caveats.**
1. The validation bank had no errors, so the confidence cutoff chosen on it is 0. A cutoff learned on one easy bank
   doesn't carry over, which is why a new bank should start at full review (burn-in) until it proves out.
2. The loan-to-value rule was added **after** looking at the test error it catches. It's a standard domain check,
   but the 0 is optimistic until confirmed on a fresh unseen bank (planned: a new test bank with a new style).

### Classifier gotcha
The Document Intelligence classifier build failed with `TrainingContentMissing` ("Could not find any training
data") until each training PDF had its Layout result next to it as `<file>.pdf.ocr.json`. Ruled out first: ADLS
Gen2 vs plain blob storage, managed identity vs SAS, folder prefix vs file list, blob content type.


### Document classifier (split + classify whole loan files)

Trained on the training banks' documents (9 types, 16–75 examples each); evaluated on whole loan files from the
unseen validation and test banks with `split="auto"`.

| Banks | Pages correct | Documents correct (type + exact page range) |
|---|---:|---:|
| Validation (B4) | 100% | 100% |
| Test (B5–B6) | 100% | 100% (500 pages / 500 documents across val + test) |

**Read this as "the synthetic task is too easy", not "the classifier is perfect."** Every synthetic document is one
page with a distinctive title. Real loan files have multi-page documents, pages without titles, and unrelated pages
(cover sheets, title policies, blank scans). Next: a harder classification set with those, plus an `other` class.

### Augmentation (v2 = v1's training banks + 20 randomized banks)

| Model | Validation recall / wrong | Test recall / wrong | Val + test recall |
|---|---|---|---:|
| v1 (3 training banks) | 99.5% / 0 | 86.9% / 4 | **91.1%** |
| v2 (+20 augmented banks, 806 docs) | 72.6% / 6 | 89.7% / 1 | 83.9% |

Augmentation is not a free win. It fixed the test bank that broke v1 (S4 maturity date 0% → 96%, no more payment-
read-as-principal) but broke the validation bank, which prints money without a "$" (a format the augmentation pools
excluded to avoid leaking it): v2 returned nothing for most payment and balance fields there, and five times took
the monthly payment as the principal (confidence 0.25–0.51). The randomly omitted fields and distractor amounts seem
to teach "take the nearest dollar figure". **Model selection is by validation, so v1 stays.** Next ablation: v3 =
augmentation without omission/distractors.

### Classifier (split + label whole loan files)
100% of pages and 100% of documents (type + exact page range) on validation and test, 500/500. **Treat as "the test is
too easy", not a win**: every synthetic document is one page with a title that doesn't change across banks. Real loan
files have multi-page documents, missing title pages and consecutive documents of the same type. Harder synthetic
packets are needed before this number means anything.

### Final holdout (B7, new style S6): scored once with v1, rules and routing frozen
| | Recall | Wrong | Silent errors (routing + reconciliation) |
|---|---:|---:|---:|
| As frozen | 86.1% | 27 | **18 / 194 (9.3%)** |
| After fix (post-hoc, not a clean test) | 100% | 0 | 0 |

The holdout bank prints dates day-first (`06.04.2008` = 6 April). **The model read every date correctly; the
normalizer assumed month-first** and turned them into June 4, at high confidence, so nothing flagged them. This is the
dangerous kind of error: correct-looking, quietly wrong. Fix (general, not bank-specific): infer each bank's date order
from its own unambiguous dates (a first number above 12 proves day-first), and send ambiguous numeric dates to review
until the order is proven. The holdout is now spent; a fresh one is needed to confirm the fix.

**Takeaway for U1:** on unseen banks the pipeline reaches 0 silent errors with ~15% of values reviewed, but only after
three safety layers (validity rules, reconciliation, bank-level format checks), and every one of those layers was found
by a held-out bank breaking something. That's the argument for new-bank burn-in in production.

## Phase 3: risk flags from free text (U2)

Servicer comment logs, borrower letters and credit memos, read from the Layout OCR text (scan noise included),
split into segments. Training notes are written in styles S1/S2; validation (S3) and test (S4, S5) use note-writing
styles never seen in training. Decoy sentences ("no bk on file per search") are in every set.

| Method | Validation P / R | Test P / R | Decoys flagged |
|---|---|---|---|
| Keyword lexicon (from training notes) | 100% / 68% | 100% / 45% | 0 |
| TF-IDF + logistic regression | 100% / 58% | 100% / 47% | 0 |
| Zero-shot LLM (gpt-4.1-mini, definitions only) | 79% / **100%** | 88% / **100%** | 0 |

The precise methods are never wrong but miss half the flags at a new bank (new abbreviations, new phrasing). The LLM
finds every flag but adds false alarms (mostly "hardship" read into routine payment notes). Every flag quotes its
segment; 95–100% of quotes were found verbatim in the page text.

**Routing rule (hybrid):** auto-accept a flag when the LLM and a precise method agree; send LLM-only flags to a person
with the quoted sentence.

| | Auto-accepted (wrong) | Sent to a person (real / false alarm) | Share reviewed | Recall after review |
|---|---|---|---:|---:|
| Validation | 21 (0) | 18 (10 / 8) | 46% | 100% |
| Test | 25 (0) | 31 (24 / 7) | 55% | 100% |

Caveat: rare flags have tiny support on test (bankruptcy 3, draw without inspection 3, fraud 1, property damage 1),
so per-flag numbers for them are anecdotes, not estimates. Oversampling rare flags is the next data change.

## Second holdout (B8, style S7), scored once with everything frozen (2026-11-11)

Built to be harder: slash day-first dates (`06/04/2008`), label wording that appears nowhere else, 2-page payment
histories, ~40% of documents without a title, and two borrower letters in a row.

| | Result |
|---|---|
| Rules | 0% recall |
| Custom model v1 | 87.6% recall, 99.4% precision (1 wrong of 193) |
| Bank date order | inferred day-first from the bank's own dates (correct) |
| Routing (confidence + rules + reconciliation + LTV) | **0 silent errors, 13.5% of values reviewed** |
| Classifier | pages 100% correct type; documents 84.5% exact |

This is the clean confirmation the first holdout couldn't give: the date-order and plausibility fixes hold on a bank
none of them were tuned on. Misses are concentrated in monthly payment (40% recall, label "Installment Amount"), and
they are misses, not wrong values.

**Classifier on hard packets:** every page got the right type, and missing titles and back-to-back letters were
handled, but **every 2-page payment history was split into two documents**: the classifier only ever saw 1-page
documents in training. Fix = multi-page training examples (a "merge consecutive same-type pages" rule would wrongly
join the two letters).

## Phase 8 (U3): terms from REAL purchase-and-assumption agreements (2026-11-11)

24 public P&A agreements from 2009–2011 failures (seeded random sample, plus earlier downloads), scanned PDFs of
57–199 pages. Ground truth: each bank's public FDIC bid summary (buyer, deposit premium, asset premium/discount,
loss-share tranche-1 percentages) and the failure date. 22 had a usable winning-bid row.

Pages chosen from the PDFs' existing text layer (free): cover pages, the bid sentence ("has submitted to the Receiver
… bid"), and the first loss-share percentage page of each shared-loss agreement. **97 of 2,839 pages (3%) analyzed;
Document Intelligence Layout ≈ $1 total.** Then gpt-4.1-mini with JSON output, where every value must carry a verbatim
quote (checked against the Layout text).

| Field | LLM correct | LLM wrong | Regex correct | Regex wrong |
|---|---:|---:|---:|---:|
| Buyer (assuming institution) | 22/22 | 0 | 1/22 | 1 |
| Effective date | 22/22 | 0 | 0/22 | 0 |
| Deposit premium % | 18/21 | 0 | 15/21 | 1 |
| Asset premium / (discount) $ | 16/20 | 0 | 11/19 | 0 |
| Loss share (yes/no) | 17/22 | 0 | 18/22 | 4 |
| Single-family loss share % | 18/21 | 0 | 10/21 | 0 |
| Commercial loss share % | 19/21 | 0 | 8/21 | 0 |

**No wrong values from the LLM on real documents**; every miss is a blank (the term was on a page the selection didn't
reach: older agreements state loss-share percentages in exhibits further back). Quotes: 100% verbatim for the numeric
fields; 4 of 17 loss-share quotes were paraphrased rather than copied, which the quote check catches.

Lessons from getting the measurement right (all found by reading misses, not by tuning on results):
- The agreement template changed over time: Article VII is "Bid; Initial Payment" in 2010–2011 versions and "First Loss
  Tranche" in 2009 versions, and the bid sentence word order varies, so pages are chosen by the sentence, not the title.
- The bid summaries aren't uniform either: some say "Winning Bid and Bidder", some "Winning bid", and large-bank deals
  (AmTrust) use a different table ("Loss Share Pool A/B"). Two scorer bugs came from that, not from the model.

## v3: augmentation without omission or distractors (2026-11-11), and the model decision

Same 20 randomized banks as v2 (identical random draws), but with no omitted fields and no distractor amounts.

| Model | Validation recall / wrong | Test recall / wrong | Holdout B7 | Holdout B8 |
|---|---|---|---|---|
| v1 (3 training banks) | 99.5% / 0 | 86.9% / 4 | 100% / 0 | 87.6% / 1 |
| v2 (+ omission & distractors) | 72.6% / 6 | 89.7% / 1 | — | — |
| **v3 (+ plain augmentation)** | 96.4% / 5 | **99.7% / 1** | 98.5% / 0 | **99.5% / 1** |

So v2's collapse came from omission and distractors, not from augmentation itself: varied layouts and wording on their
own carry over to unseen banks. v3's validation errors were 4 principal/payment swaps and 1 partial date
("Jan-2011"); two general rules found on validation (payment must be 0.1–6% of principal; a date needs a day) catch them.

After routing (confidence + validity rules + reconciliation + plausibility), both models reach **0 silent errors on
every bank**. The difference is human effort:

| Review load | Validation | Test | Holdout B7 | Holdout B8 |
|---|---:|---:|---:|---:|
| v1 | 9.1% | 14.7% | 0.0% | 13.5% |
| **v3** | 11.7% | **2.3%** | **1.5%** | **1.6%** |

**Decision: v3 is promoted** (loaded into Azure SQL and Power BI). Caveat: the two holdout banks informed this choice,
so they're no longer untouched; a fresh bank is needed for a final, unbiased estimate.

## Phase 6: the review workflow, running (2026-11-11)

- **Power Apps review app** ("Loan File Desk Review", canvas app on Azure SQL, Entra sign-in; source in `powerapps/src`,
  deployed by `powerapps/deploy.sh install` from git). The reviewer sees the watermarked page, the extracted value,
  confidence, why it was flagged, what's printed on the page, and the data-room value, then Accepts, Corrects, or marks
  "Not on document". Screenshot: `docs/img/powerapps-review.png` (first item: the model read the monthly payment as
  the principal; the data room says $1,051,000).
- **Power Automate** (solution-aware, defined in `powerapps/flows/add_flows.py`, bound to the SQL connection at import):
  "Apply review decisions" every 15 minutes runs `desk.apply_review_decisions`, which turns decided items into
  `desk.review_decision`, closes the queue item, writes a **new version** of the loan-tape value (temporal table; the
  model's value stays in history) and an audit event. "Review SLA escalation" hourly moves past-SLA items to priority 0.
- Verified: a correction typed in the app became the current loan-tape value with the model's value in history; both
  flows ran on their own schedule (apply at 05:23 UTC, 6 items escalated at 05:24).
- Gotchas: the SQL connection's sign-in had expired (flows import but can't be activated until the connection is
  reconnected); `pac auth create --deviceCode` is blocked by the tenant's security defaults, so use the browser flow.

## Classifier v2: hard-packet training banks (2026-11-11)

Three small training banks (H01–H03, 30 loan files) drawn from the training-only pools, with the "hard packet"
features: 2-page payment histories, ~40% of documents without a title, and back-to-back borrower letters.

| Classifier | Validation docs | Test docs | Hard holdout B8 docs |
|---|---:|---:|---:|
| v1 (1-page training documents) | 100% | 100% | 84.5% (every 2-page payment history split in two) |
| **v2 (+ hard-packet banks)** | 100% | 100% | **100%** (161/161) |

Caveats: B8 is the bank that exposed the problem, so this confirms the fix rather than giving an unbiased score;
and the hard-packet banks come from the same generator as B8, so real files (longer, messier) remain harder.

### U3 update: Pydantic structured outputs (2026-11-11)

The agreement extractor now uses Azure OpenAI **structured outputs** with a Pydantic schema (`schemas.PATerms`): every
term must come back as value + verbatim quote + page, a value without a quote is dropped, and a loss-share percentage
must be quoted from text about loss sharing. Re-run on the same 22 agreements:

| Field | Free-form JSON (before) | Structured + validated (now) |
|---|---|---|
| Buyer / effective date | 22/22, 22/22 | 22/22, 22/22 |
| Deposit premium / asset discount | 18/21, 16/20 | 18/21, 16/20 |
| Loss share (yes/no) | 17/22 | 17/22 |
| Single-family loss share % | 18/21 | 18/21 |
| Commercial loss share % | 19/21 | 16/21 |
| **Wrong values (all fields)** | **0** | **0** |

Structured output made the model more cautious on one field (3 more blanks). It also produced one wrong value, a 50%
quoted verbatim from a clause about charged-off asset collections; the quote check passed because the text is real, so
the schema now also checks **relevance** (a loss-share percentage needs a loss-share quote), which rejects it. A blank
goes to a person; a wrong number in a loan sale doesn't, so this trade is the right one.

## Final clean holdout: B9 (2026-11-11), scored once

The earlier holdouts helped choose v3, so a third bank was generated **after** everything was frozen (code at commit
b3de760: model v3, classifier v2, routing rules), with a style used nowhere else: "06 Apr 2008" dates, "1,051,000.00
dollars" amounts, labels like "Note Amount", "Loan Ends", "Valued At", and a two-column layout. Scored once.

| | Result |
|---|---|
| Rules (training-bank wording) | 0% |
| Classifier v2 | 100% of pages and documents (159/159) |
| **Model v3** | **197/197 values correct (100% recall, 100% precision)** |
| Routing (confidence + rules + reconciliation + plausibility) | 0 silent errors, 2.0% reviewed (the bank's planted schedule mismatches) |

Why I believe it rather than suspect it: the model was unsure but right (median confidence 0.64 vs ~0.9 at familiar
banks), half the values came from scanned pages, and the new formats are unambiguous. **What it supports:** with 0 errors
in 197, the error rate at a bank like this is very likely below ~1.5% (rule of three: 3/197). It is one synthetic bank,
not proof about real loan files.

## Candidate loan-level rules (offline, 2026-11-16)

Amortization, term, dates against the closing, balance against the note's schedule, and two cross-document checks
(servicing balance vs the note's principal; loan-to-value from the note's principal). Measured offline in
[RULES_V2.md](RULES_V2.md), **not wired into routing**. Real misreads on unseen banks (neural v1–v3): current rules
catch 10 of 19, current + new 16 of 19. Planted misreads: 38% → 71%. No new false alarms on loans without a misread.
The synthetic loans follow the textbook formulas, so real loans will need looser tolerances.
