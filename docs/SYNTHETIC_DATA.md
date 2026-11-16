# Synthetic data: standing in for records that aren't public

A receiver's real work runs on records the public never sees: full borrower packets, underwriting
memos, construction draw files, exam reports, data-room loan schedules. They're protected by privacy
law and confidentiality rules, and only vetted staff with a need to know can see them. This project
**generates fictional stand-ins** for those record types, so the pipeline faces the same variety of
documents, with known correct answers planted in each one.

## Ground rules (every generated document)
- **Fictional everything:** bank names, people (Faker), addresses, employers. SSNs/TINs only in invalid
  ranges (e.g. 9xx-xx-xxxx), account numbers clearly fake. No real person or bank is depicted.
- **Watermark** "SYNTHETIC — FOR RESEARCH" on every page. Metadata records the generator version and seed.
- **Planted truth:** every field the pipeline should extract is stored in a labels file next to the document.
- **Realism anchors, not real records:** sizes and mixes from real failed banks' public Call Reports; blank
  public forms as layouts; optional public loan-level datasets for realistic rates, LTVs and delinquency paths.
- **Layouts vary by fictional bank** (fonts, field order, scan noise, skew, stamps, handwriting-style notes),
  so held-out banks test what the real job would: documents the model hasn't seen.
- LLMs write the narrative text (memos, notes, letters) from a structured "story" for each loan, so the text
  and the numbers agree, and the flags are planted on purpose.

## Document families (built in tiers)

| Tier | Family | Documents | Pipeline feature it exercises | DI model |
|---|---|---|---|---|
| **A (MVP)** | Core loan file | Promissory note, deed of trust, appraisal (1004), Closing Disclosure, payment history | Classification, field extraction, reconciliation | prebuilt mortgage + custom neural |
| **A** | Servicing text | Servicer comment log, borrower letters | Text risk flags (bankruptcy, hardship, litigation, fraud, damage, payoff) | Layout → text models |
| **A** | Underwriting judgment | **Internal credit memo** (loan officer narrative, exceptions approved, risk rating, committee decision) | Text: "approved despite…" exceptions, DTI/LTV stated vs computed | Layout → text models |
| **B** | Borrower financials | W-2, Form 1040 (summary pages), bank statements, pay stubs, credit report summary | Prebuilt financial models; **cross-document checks** (income on 1003 vs W-2 vs 1040; DTI recomputed) | prebuilt tax / bank statement / pay stub (verify names at build time) |
| **C** | Commercial real estate + construction | Rent roll, operating statement, construction budget + draw request, lien waiver, inspection report, participation agreement, environmental report summary | Tables, multi-page extraction; draw vs budget vs inspection reconciliation; participation splits | Layout (tables) + custom neural |
| **D** | Problem-loan and supervisory (fictional) | Watchlist / classified asset report (pass, special mention, substandard, doubtful, loss), workout memo, forbearance agreement, default notice, exam-style findings narrative | Risk-rating extraction; text themes; consistency (watchlist rating vs memo vs payment history) | Layout + text models |
| **D** | Data-room loan schedule | The loan-level schedule a bidder would see | **Reconciliation target**: the loan tape we produce is checked against it | (structured) |
| Out of scope | Depositor and core-banking records | Uninsured depositor schedules, ledgers, system backups | Not loan files; skip | — |

**Order:** Tier A first: it's enough for the full demo (classify → extract → flag → review → release).
Tier B adds the strongest "AI catches what people miss" story (income doesn't match across documents).
Tiers C and D add commercial real estate (the loans that actually sank most 2008–2012 failures) and
problem-loan text.

## One loan, one story
Each synthetic loan starts as a structured story, and every document is rendered from it:

```yaml
loan_id: FB07-000123
bank: Lakeside Community Bank (fictional), layout L3
type: construction            # drawn from the real failed bank's Call Report mix
original_principal: 2400000
note_rate_pct: 7.25
stated_income: 410000         # on the 1003
w2_income: 265000             # planted mismatch → DTI exception
risk_rating: substandard      # watchlist
events:
  - 2008-09: draw 6 approved without inspection     # credit memo + draw file
  - 2009-03: borrower letter requests forbearance   # hardship flag
  - 2009-07: comment log notes bankruptcy filing    # bankruptcy flag
truth_flags: [hardship, bankruptcy, income_mismatch, draw_without_inspection]
```

The documents are rendered from the story (forms filled in, narratives written by an LLM from the
events), then scanned-looking noise is added. Labels come straight from the story, so scoring is exact.

## Cost
Narrative text via a small model, cached per story; forms rendered locally (HTML → PDF, as in the sibling
insurance-docai generator). Target: a few dollars for a few thousand pages. DI extraction cost is
estimated per batch before running.

## Status and known limits (2026-11-10, seed 7, 25 loans per bank)
- 150 loan files, 978 pages; about half the documents "scanned" (rasterized, skewed, blurred, JPEG).
- Loan mix per bank copies a real 2010 failure's last Call Report (commercial real estate is the largest share).
- Planted text flags: hardship 60, underwriting exception 30, payoff 22, litigation 15, bankruptcy 8,
  draw without inspection 6, property damage 5, fraud 2; plus 53 decoy sentences. **Rare flags (fraud,
  damage, bankruptcy) are too few to measure per-flag recall on test banks**: raise `--loans` or oversample
  them before phase 3.
- Borrower-letter bodies don't yet vary by style (only the opening does); the comment logs and memos do.
  An LLM paraphrase pass is a planned improvement if the text models find the letters too easy.
- 31 loans had already matured before the failure date (realistic for a failing bank's book).
- Tier B/C/D documents are not generated yet.
