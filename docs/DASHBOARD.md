# Dashboards (Power BI): accessible and actionable

Two reports, both on **Azure SQL** (the serving layer). Databricks builds the tables and Power BI reads them.

- **Answer report:** one page per use case (U1–U3), plus a context page on the 2008–2012 and 2023 failures.
- **Operations report:** monitoring the document pipeline and review workflow, for whoever runs it.

## Designing for an audience we don't know yet

The users aren't known yet. So instead of guessing roles, each page starts from **a question someone
would ask and the action the answer should trigger**. Real usage then tells us who actually shows up.

1. **Question-first pages.** Every page title is a question; every visual's subtitle states the answer in
   plain words (a finding the data supports, written once the numbers are in), not just a metric name.
2. **Every number has a threshold and an action.** For example: "Review backlog > 2 days → reassign (button
   opens the Power App queue)". If a visual can't lead to an action or a decision, it doesn't go on the
   operations report.
3. **Audience hypotheses, then evidence.** Start with three hypothetical users: *the curious reader*
   (the answers), *the operator* (is the pipeline healthy?), *the lead* (are we on track, what's at risk?).
   Turn on Power BI **usage metrics** and track which pages and filters get used; drop or merge what nobody uses.
4. **Accessibility (built in, not added later):** color-blind-safe palette; never color-only encoding (add
   icons/labels); alt text on every visual; logical tab order; minimum font 12pt; plain-language titles;
   a "How to read this page" tooltip; works at phone width (mobile layout).
5. **Trust signals on every page:** data as-of time, source, and "synthetic" labeling wherever synthetic
   data appears.

## Answer report (one page per use case)

| Page (question) | Visuals | Data |
|---|---|---|
| Context: how big were the waves? | Failures per year 1934–2026 with 2008–2012 and 2023 highlighted; estimated cost to the insurance fund | Failures API |
| **U1: How fast can loan files become a trusted loan tape?** | Pages/hour, review load and silent-error rate per held-out bank; burn-in curve (review % falling as a new bank proves out) | Pipeline + review tables |
| **U2: Which loans are in trouble?** | Loans by flag and pool; per-flag precision/recall; method comparison (TF-IDF vs transformer vs LLM: accuracy and cost); drill-through to the quoted sentence | Text flags |
| **U3: What did the resolution deals say?** | Deals by year and type; share with loss-share; deposit premium / asset discount distribution; drill-through to the clause | Real P&A extractions |

## Operations report (monitoring)

| Page (question) | Measures | Threshold → action |
|---|---|---|
| **Is the pipeline healthy right now?** | Runs today, failed runs, stage latency (p50/p95), Document Intelligence errors/throttling, queue depth | Any failed run or p95 > 2× baseline → alert (Power Automate) to the on-call owner; link to the run log |
| **Is the data trustworthy?** | Estimated silent-error rate, reconciliation mismatch rate, quote-verification failures for text flags, % auto-accepted | Silent errors > 2% for a bank → that bank goes back to full review |
| **Is a new bank behaving differently? (drift)** | Confidence distribution and reviewer-correction rate per bank and document type vs the validated baseline | Correction rate rising for 3 days → retrain or extend burn-in |
| **Is review keeping up?** | Backlog by age, items past SLA, reviewer throughput, decisions mix (confirm/correct/not on doc) | Past-SLA > 0 → escalation flow; backlog trending up → add reviewers |
| **Is it faster and more accurate than keying by hand?** | Error rate against the answer key (model alone, after review, keyed by hand); human seconds per field (review app: `opened_at` + `seconds_on_item` on every decision; manual: the `/manual` page times a person keying the same documents); share of fields with no human touch; hours from file received to loan ready | Manual error rate or time beats the AI process for a field or bank → route that field to full review and investigate |
| **What is it costing?** | DI pages and cost, LLM tokens and cost, Databricks usage, Azure spend vs budget, **cost per 1,000 pages** | Forecast > budget → pause batch runs |
| **Is the data fresh?** | Last successful refresh per source (FDIC API, Call Reports, CFPB) | Older than expected → alert |

**Where ops data comes from:** pipeline workers write `run_log` / `stage_metric` / `review_sla` rows to Azure
SQL; Log Analytics holds service logs and metrics (Document Intelligence, Functions); daily cost goes to
`ops.cost_daily`. Planned, not built: a daily run that pulls Log Analytics into `ops.daily` (empty today). Alerts: Azure Monitor alerts for
infrastructure; Power BI data alerts → Power Automate for business thresholds (backlog, silent errors).

## Turnaround and correctness vs keying by hand (built)

`src/turnaround.py` adds the measures; Power BI page "Is it faster and more accurate than keying by hand?" shows them.

| KPI | Definition | Source |
|---|---|---|
| Model error rate | Wrong or missing values ÷ values, before review | `eval.field_outcome` (planted answer key) |
| Final error rate | Wrong values nobody caught (silent errors) ÷ values; for manual keying, keyed values that don't match the answer key | `eval.field_outcome`, `dash.v_manual` |
| No human touch | Share of values accepted without a person | `eval.field_outcome.routed` |
| Human seconds per field | AI: average review seconds per item × share sent to review. Manual: seconds per document ÷ fields on it | `dash.v_touch`, `review.manual_entry` |
| Cycle time | Hours from loan file received to no open review items | `dash.v_cycle` |

Review time over 30 minutes on one item is treated as a reviewer who walked away and isn't counted. Manual keying
uses `/manual` on the Q&A web app: a person presses Start, keys the values from the page image, and saves; the
clock runs only while the page is shown. Values are scored to half a cent (dates exactly); a value that isn't printed
on the page should be left blank. Item ages in the queue are simulated, so cycle time is only meaningful for loans
reviewed after a reload. The review app also sends these timings to Application Insights with `Trace`, but those
events weren't arriving (checked 2026-11-14), so SQL is the source of truth.
