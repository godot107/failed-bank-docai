# failed-bank-docai

Personal research project on the **2008 recession and the mortgage bubble**: what the 2008–2012 wave of
bank failures looked like, why the banks failed, how they were resolved, and what borrowers went through.
It's built from the public record, and includes **Receivership Loan File Desk**, an application that
simulates how a receiver turns a failed bank's loan files into trusted data (Azure AI Document
Intelligence + text models + Power Apps/Automate + Azure SQL + Power BI; Databricks is planned, not built).

Status: scaffold. Plan: `docs/PLAN.md`. Product: `docs/APPLICATION.md`. Data: `docs/DATA_SOURCES.md`,
`docs/DATA_MODEL.md`, `docs/SYNTHETIC_DATA.md`. Dashboards: `docs/DASHBOARD.md`. Background notes: `private/CONTEXT.md` (not tracked).

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python src/fdic_sources.py --docs 6                     # recent failures
.venv/bin/python src/fdic_sources.py --years 2008-2012 --limit 50 # crisis-era P&A agreements
.venv/bin/pytest -q tests

# Azure (nothing deployed yet). Person-specific values come from the shell, never from git:
ALERT_EMAIL=... OWNER_TAG=... infra/up.sh --what-if   # compile / preview
ALERT_EMAIL=... OWNER_TAG=... infra/up.sh             # deploy dev (DI_SKU=F0 default, DEPLOY_AI=false default)
infra/down.sh                                         # delete the dev RG (asks first)
.venv/bin/python tools/run_sql.py sql/001_schema.sql    # once, on a fresh database (Entra auth)
.venv/bin/python src/load_sql.py && .venv/bin/python src/review_projection.py --reviewer <work UPN>   # results → SQL
.venv/bin/python src/turnaround.py --reviewer <work UPN>   # timing columns, answer key, AI-vs-manual views (rerun after review_projection)
.venv/bin/python src/powerbi.py publish && .venv/bin/python src/powerbi.py report                     # Power BI (publish --recreate after adding a table)
powerapps/deploy.sh install          # review app + 2 flows (pac profile fbd-dev; turn flows on once in Power Automate)
.venv/bin/python tools/build_notebook.py                                                               # walkthrough
# Loan Desk Q&A (Foundry agent + web page; README "Run it" has the order):
.venv/bin/python src/knowledge.py ; .venv/bin/python src/agent.py create|ask "..." ; .venv/bin/python tools/ask_eval.py
infra/ask_auth.sh ; infra/ask_up.sh ; app/deploy.sh      # teardown: infra/down.sh + infra/ask_auth.sh delete (tenant-level)
```

Loan Desk Q&A (2026-11-12): agent `loan-desk-analyst` in Foundry project `loan-desk` on the existing AI account;
Function App `func-fbd-*` serves `/desk?loan=<number>` + `/api/ask`, and `/manual` (key a document by hand, timed: the human baseline) behind Entra sign-in (app registration
`fbd-loan-desk-ask`). After changing INSTRUCTIONS or tools: `src/agent.py create` (the app's cache key includes the
agent version). Knowledge = SQL full-text over `kb.passage` (no search service): loan files, P&A pages, Resolutions
Handbook, FDIC pages, Crisis and Response chapters. Guardrails `src/guardrails.py` (Prompt Shields on document text,
figure + quote checks); red team 2/18 → 0/18. Queue priority = SQL procedure + trigger on review.item
(`sql/007_prescriptive.sql`; rerun `src/prescribe.py` after a demo reset). Mining `src/failure_mining.py` → kb.mining_*.
Eval 20/20. Docs: docs/LOAN_DESK_QA.md.

## Decisions / constraints

- **Public framing is neutral.** Tracked files must not name or imply any employer, client or
  person. Personal research interest only. Public agencies (FDIC, CFPB, Inspectors General) are named only as
  **data sources**, with "not affiliated with or endorsed by" wording. Check with `tools/neutral_check.sh` before
  any commit (its term list is `private/neutral_terms.txt`, not tracked).
- **Data:** real public documents and data wherever they exist; loan files, comment logs and the servicing
  system are **synthetic** with planted ground truth. Never use real borrower data. Drop company names from
  complaint data in anything published.
- Single sources of truth: `src/fdic_sources.py` (URLs + fetchers), `schema/loan_fields.yaml` (document types,
  fields, text flags).
- Evaluate **by bank** (held-out banks), never with a random split.
- Stack: Azure (Document Intelligence, Blob/ADLS, Functions, Key Vault, Azure SQL, Log Analytics), Databricks (NOT BUILT; planned:
  Unity Catalog over storage + Azure SQL, MLflow), Power Platform (Power Apps model-driven + Dataverse, Power Automate, Power BI).
- Azure: mirror ../insurance-docai (RG per env, tags per `../../shared/TAGGING.md`, budgets, Entra-only auth,
  free-trial spending limit stays ON, no pay-as-you-go upgrade without the owner's approval).
- HTTP User-Agent = project name + contact@willieman.com (workspace rule: no personal email).
- Repo: PUBLIC on GitHub since 2026-11-17 (origin, branch master; commits use the GitHub noreply email). Re-run `tools/neutral_check.sh` before every push.
