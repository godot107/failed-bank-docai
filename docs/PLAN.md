# Plan

A personal research project on bank failures, from the **2008 mortgage bubble** to the **2023 Silicon
Valley Bank run**, plus an application (**Receivership Loan File Desk**, `APPLICATION.md`) that simulates
how a receiver turns a failed bank's records into trusted data.

## Scope: three real use cases, each answering one question

The scope is limited to work a receiver actually does, as publicly documented in the FDIC Resolutions
Handbook and Inspector General reports. Everything built must help answer one of these questions.

| # | Real-world use case (public source) | The question we answer | Success measure | Data |
|---|---|---|---|---|
| **U1** | **Loan data for bidders under time pressure.** Bidders do due diligence in a virtual data room; the Handbook notes that when time is short there may be no time to image loan files at all. A liquidity failure (a run, like SVB in 2023) compresses ~90 days of preparation into days. | **How many loan files per hour can become a trusted loan tape, and how much human review does that take, at ≤2% silent errors on a bank the model has never seen?** | Field precision/recall on held-out banks; silent-error rate; review load; pages/hour; cost per 1,000 pages | Synthetic Tier A loan files sized from real failed banks |
| **U2** | **Problem-loan triage for valuation and loan sales.** Retained loans are valued and sold in pools; problem loans (bankruptcy, litigation, hardship, underwriting exceptions) change value and handling. | **Can risk flags read from free text (servicer notes, letters, credit memos) sort loans into the right pools, with every flag backed by a quoted sentence?** | Per-flag precision/recall; % of flags with a verified quote; TF-IDF vs transformer vs LLM cost and accuracy | Synthetic Tier A text (+ Tier B income checks) |
| **U3** | **Resolution-agreement terms across banks.** 2008–2012 deals used purchase-and-assumption agreements, many with multi-year loss-share terms that had to be administered and monitored. | **Can the key terms (buyer, date, deposit/asset premium or discount, loss-share coverage) be pulled reliably from 60–170-page scanned agreements and compared across the wave?** | Accuracy against facts we can check (buyer, date from the failures API) + a hand-checked sample; pages analyzed vs pages in the file | **Real** public P&A agreements, 2008–2012 |

**Context, not a deliverable:** one page showing the scale of each wave (2008–2012: 465 failures; 2023: SVB,
Signature, First Republic), from the failures API.

### Cut from scope (would not help answer U1–U3)
- Comparing failed banks with surviving peers: supervision research, not receivership work.
- CFPB complaint analysis: post-crisis servicing, not receivership. CFPB text may be used **only** as extra
  training text for U2 flags, if the synthetic text proves too clean.
- Inspector General post-mortems: optional domain reading; no pipeline.
- Tier C/D synthetic documents, except the **data-room loan schedule** (U1's reconciliation target).
  Commercial real estate and construction files are a stretch goal after U1–U3.
- Depositor and core-banking records.
- Fannie Mae / Freddie Mac loan-level data: not needed for U1–U3.

## Stack

| Layer | Tools | Serves |
|---|---|---|
| Documents | Azure AI Document Intelligence: classifier, prebuilt mortgage, custom neural (U1); Layout (U3) | U1, U3 |
| Text | Text classifier (TF-IDF → small transformer → LLM with quotes) | U2 |
| Ingestion + transformation | **Databricks**: public sources + pipeline outputs → bronze/silver/gold; reconciliation; MLflow; daily ops job | U1–U3 |
| Serving | **Azure SQL**: loan tape, extractions, versioned values, review decisions, audit, ops tables | U1–U3 |
| App + workflow | **Power Apps** (model-driven, Dataverse) + **Power Automate** (intake, assignment/SLA, release approval, daily status, alerts) | U1, U2 |
| Dashboards | **Power BI**: one answer page per use case + the **operations monitoring** report (`DASHBOARD.md`) | U1–U3 |
| Platform | Blob/ADLS, Functions (Python), Key Vault, managed identity, Log Analytics + alerts, Bicep, budgets | all |

## Phases (application first)

| # | Phase | Answers | Status |
|---|---|---|---|
| 0 | Scaffold, data sources, design, synthetic-data spec, F0 chunker | — | **done 2026-11-10** |
| 1 | Synthetic Tier A loan files for 6 fictional banks (layouts differ), loan mix from real failed banks; data-room schedule | U1, U2 | **done 2026-11-10**: 150 loan files, 978 pages (train 75 / val 25 / test 50; test styles unseen), 150 planted text flags + 53 decoys, 16 data-room mismatches; `src/synth/generate.py` |
| 2 | Split + classify + extract (DI); evaluate on held-out banks | U1 | **done 2026-11-11** (docs/RESULTS.md): rules 0% on unseen banks; neural v1 87–99.5%; v2 augmentation trade-off; classifier 100% (too easy); holdout found a date-order bug |
| 3 | Text flags (TF-IDF → transformer → LLM), quote verification | U2 | **done 2026-11-11**: keywords/TF-IDF 100% precision ~50% recall; LLM 100% recall 79–88% precision; hybrid = 0 wrong auto-accepts, 100% recall, ~half reviewed (transformer arm not run) |
| 4 | Validate + reconcile vs the data-room schedule; review routing with new-bank burn-in | U1 | **first pass done 2026-11-11**: 0 silent errors / ~15% review on test (post-hoc rules; fresh holdout needed) |
| 5 | Azure infra (Bicep) + Azure SQL serving + Databricks medallion | U1–U3 | **Azure deployed 2026-11-10** (rg-failed-bank-docai-dev; DI S0 eastus; SQL `loandesk` schema applied; DI smoke test OK); Databricks todo |
| 6 | Power Apps + Power Automate | U1, U2 | **done 2026-11-11**: canvas review app (LoanFileDesk solution, managed as code) + 2 scheduled flows (apply decisions every 15 min, SLA escalation hourly) running SQL procedures; verified end to end |
| 7 | Power BI: U1/U2 answer pages + operations monitoring report | U1, U2 | **published 2026-11-11** (4 pages: trust, review queue, pipeline health/cost, failure context; docs/img/powerbi) |
| 8 | Real P&A term extraction (10-agreement sample first, then the wave) | U3 | **first pass done 2026-11-11**: 22 real agreements, 3% of pages, ~$1; LLM 0 wrong values, buyer/date 22/22 (docs/RESULTS.md) |
| 9 | Docs, report, 5-minute demo | all | **done 2026-11-11** (README, docs/REPORT.md, notebooks/walkthrough.ipynb); demo recording todo |

## Document Intelligence on the free tier (F0)
F0 analyzes only the **first 2 pages per request** and **500 pages a month**. `src/di_chunk.py` handles that:
1. **Select pages** using the PDF's existing OCR text layer (free), e.g. "deposit premium|asset premium|asset
   discount|bid amount" plus the first pages. Avoid "shared loss": in loss-share agreements that phrase sits in
   every page header, so it selects everything.
2. **Chunk** the selected pages into 2-page PDFs, analyze each, and **remap** page numbers to the original.
3. **Budget:** a full 145-page agreement uses 145 of the 500 free monthly pages. On S0 the same agreement costs
   about **$1.45 with Layout** or **$0.22 with Read** (eastus list prices checked 2026-11-10, per 1,000 pages:
   Layout/prebuilt $10, Read $1.50, custom $30, classifier $3).

Trade-offs: chunking breaks tables and sections that span chunk edges (keep contiguous runs together and add
a page of overlap where it matters). The custom classifier splits whole packets, so U1 packets stay short or run on S0.

## Evaluation rules
- Split by **bank**, never randomly. Thresholds chosen on a validation bank; reported once on test banks.
- Report precision/recall, silent-error rate and review load, not accuracy alone. Keep rules-only and TF-IDF baselines.
- Every text flag and extracted clause quotes its source, checked automatically.
- State the limits next to every result: synthetic data, OCR noise, sample sizes.

## Cost guardrails
- Azure free trial (spending limit on) ends around early December 2026. DI runs on **S0**: on the trial, charges come out of the credit (the spending limit stops anything beyond it). Page selection + an estimate before every batch.
- LLM runs: small models, stratified samples, cached; a few dollars.
- Databricks Free Edition (serverless); no idle clusters. Power Platform Developer Plan.

## Decisions (2026-11-10)
1. Fannie Mae / Freddie Mac data: **not now** (not needed for U1–U3).
2. The sibling project's public README was neutralized **locally**; pushing it is the owner's call.
3. **Application first** (phases 1–4); the context page later.
4. Committed locally; no GitHub repo yet.

## Next: run on non-person identities (not started)

Today the Function App and Document Intelligence use **managed identities**, but two things run as a person:
the Power Automate flows (SQL connector signed in as a user account; when its token expires the flows stop
until someone clicks Reconnect) and the batch scripts (`az login`, `DefaultAzureCredential`). Production
shouldn't depend on one person's login.

| Part | Change | Least privilege |
|---|---|---|
| Power Automate flows ("Apply review decisions", "Review SLA escalation") | SQL connection with **service principal** authentication | `EXECUTE` on `desk.apply_review_decisions` and `desk.escalate_past_sla` only |
| Batch runs (`load_sql.py`, `review_projection.py`, `turnaround.py`, `prescribe.py`) | Scheduled in Azure (Function timer) under a **managed identity** | Reader on the lake `raw/`, DI user, SQL roles per schema |
| CI/CD (deploy, tests) | GitHub Actions with **workload identity federation** (OIDC), no stored secret | Contributor on the dev resource group only |
| Power BI publish (`src/powerbi.py`) | **Service principal** (tenant setting "service principals can use Fabric APIs") added to the workspace | Workspace Contributor |

Then add an "Identities" section to `docs/DATA_MODEL.md`: who runs what, with which rights. About an hour, $0.
Re-test the review loop end to end afterwards (correct a value in the app → flow → `desk.loan_field` history).
