# Code and design review (2026-11-11)

Grounded in the author's textbook library (citations: book, page). **Fixed** = changed in this commit;
**Accepted** = deliberate for a dev/demo build, revisit for production.

## Defects found and fixed

| # | Where | Problem | Fix | Verified by |
|---|---|---|---|---|
| D1 | `src/load_sql.py`, `src/review_projection.py` | Every reload **deleted the audit trail**: decisions, audit events, and the loan-tape / review history tables (versioning switched off, history deleted). That contradicts the design's core promise. | `desk.audit_event` is never deleted; tables are cleared **with system versioning on**, so every value stays in history; the reset itself is an audit event; reloading a desk that holds reviewer decisions refuses unless `--reset-demo`; the projection rebuild applies pending decisions first. | Reload refused with 2 decisions present; `review.item_history` kept its 40 rows across a rebuild |
| D2 | `desk.apply_review_decisions` | Not safe to run twice at once (the 15-minute flow and a manual run could both apply a decision), and a reviewer's change of mind after a decision was applied was **silently ignored**. A clock-based fix was also wrong: the app stamps `reviewed_at` in the reviewer's local time (6 h off UTC), and temporal row times are transaction *start* times. | `sp_getapplock` serializes runs; a decision is re-applied when its **content** (status or value) differs from what was last applied (`applied_status`, `applied_value`), not by comparing clocks. | `tools/selftest_apply.py`: second run applies 0; change of mind recorded as a second decision |
| D3 | same | A corrected date that isn't `YYYYMMDD` (e.g. `2008046`) became **NULL in the loan tape**, silently. | Unreadable corrections go back to the reviewer (`status = open`, message in the comment) and are logged as `invalid_review_value`; nothing is written to the loan tape. | self-test |
| D4 | same | "Not on document" left the **model's value** in the loan tape (reason "pending review"), and recorded nothing when the model had found no value. | The value is cleared (NULL) as a new reviewer-sourced version, also when no row existed. | self-test |
| D5 | `src/review_projection.py` | The reviewer UPN was pasted into `CREATE USER`/`GRANT` statements. | UPN validated against a strict pattern and bracket-quoted; existence check parameterized. | — |
| D6 | lint (`ruff`: F, E9) | Unused imports/variables; a loop variable used after a loop that could be empty. | Cleaned; `ruff check` passes. | — |

Tests: 16 → **22** (new `tests/test_route.py`: swapped principal/payment, partial dates, ambiguous day/month, missing,
out-of-range rate) + the database self-test (`tools/selftest_apply.py`, 9 scenarios, runs in a transaction that is
rolled back).

## Design, checked against the textbooks

**1. Validity rules are not accuracy checks, so the pipeline also reconciles.** Sebastian-Coleman: validity is
"the conformance of data values to a domain or to a business rule", while accuracy "requires comparison to the actual
object" (*Measuring Data Quality*, p.374), and when two records of the same fact disagree, "at least one of them must be
incorrect" (p.104). That is exactly the split here: range/plausibility rules catch impossible values; the comparison
with the bank's data-room schedule is what caught most real errors (3 of 4 v1 test errors). **Consistent.**

**2. Thresholds and model choice on validation; test used once.** ESL separates training, validation (model
selection) and test (assessment of the final model) (*Elements of Statistical Learning*, p.241) and bases tuning on an
estimate of prediction error (p.247); Kuhn & Johnson keep a holdout "not used until a later chapter" (*Applied
Predictive Modeling*, p.315). Followed for cutoffs and for v1 vs v2. **Two deviations, both disclosed in RESULTS.md:**
the loan-to-value rule was added after seeing a test error, and the two holdout banks were used to choose v3 over v1,
so they are no longer untouched. **Accepted** with disclosure; a fresh bank is the remedy.

**3. Group (bank) splits, because new banks are the real use.** Kuhn & Johnson note that when a model must handle
*new* cases (their example: new spam techniques), evaluation should reflect that rather than random homogeneous splits
(p.80). Every evaluation here holds out whole banks with styles never seen in training. **Consistent.**

**4. Why augmentation v2 failed.** Goodfellow et al. describe dataset augmentation as valid when the class is invariant
to the transformation (*Deep Learning*, pp.475–476). v2's "omit a field / add a decoy amount" transformations changed
what the right answer was near the label (the nearest dollar amount was no longer the field), so the model learned the
wrong invariance; v3 kept only label-preserving variation (layout, wording, formats) and generalized. **Consistent with
the finding.**

**5. LLM outputs must be checked against their source.** Huyen treats factual consistency as the critical quality
for generated output, evaluated against the provided context (*AI Engineering*, pp.326, 329–330). Every LLM flag and
every agreement term here carries a quote that is checked verbatim against the page text; unverified numbers are never
auto-accepted. Huyen also recommends evaluating on production-like data with clear rubrics (p.403): the real P&A test
uses FDIC's own bid summaries as the rubric. **Consistent.**

**6. Combining a precise and a thorough text classifier.** Combining taggers by running several in parallel and voting
or arbitrating (*Jurafsky & Martin*, p.178) is the pattern behind the hybrid rule (auto-accept on agreement, person on
disagreement). **Consistent.**

**7. Lineage and audit.** DAMA-DMBOK describes lineage as how data is created, moved and changed, including "the rules
according to which data is changed" (*DMBOK*, p.291). Here: original file (hashed, write-once) → extraction (model,
version, confidence, page) → loan-tape value (temporal: every version, who, why) → decision and audit event. D1 was the
gap: a reload could erase that history. **Fixed.**

**8. Scheduled jobs must tolerate repeats and overlap.** "An idempotent task produces an effect the first time it is
run but when repeated produces no additional effects" (*Cloud Application Architecture Patterns*, p.206), and an app
must work with multiple copies running at once (p.138). The apply flow now meets both (D2). The SLA escalation is
idempotent by construction (only items not already at priority 0). **Fixed / consistent.**

## Follow-up: typed contracts at the trust boundaries (Pydantic, 2026-11-11)

Model output is untrusted input, so it is validated where it enters (`src/schemas.py`):
- **LoanTape**: the validity and plausibility rules now live on the loan record (range constraints + cross-field rules
  reported per field), instead of being spread through the router. Routing decisions for 972 values were checked to be
  **identical** before and after the move, for both models.
- **PATerms**: the Azure OpenAI structured-output schema for agreement terms; unquoted values dropped; a relevance rule
  for loss-share terms caught a verbatim-but-wrong quote that the plain quote check let through (RESULTS.md, U3 update).
- **TextFlagReply**: flag names restricted to the known set; a malformed reply is logged and treated as "no flags"
  instead of crashing or leaking unknown labels. All 179 cached replies validate; results unchanged.

Tests: 22 → **30** (`tests/test_schemas.py`).

## Accepted for the dev build (revisit for production)

| Item | Why it's acceptable now | Production change |
|---|---|---|
| SQL firewall allows Azure services (0.0.0.0 rule) | Power BI, Power Apps and Power Automate connect from Azure; Entra-only auth still required | Private endpoint + VNet data gateway; Power Platform managed environment with VNet support |
| Flows and app run on the owner's Entra connection | Single-user demo | Service principal / dedicated connection, connection references per environment |
| Power BI push dataset, no row-level security | One fictional dataset | Import/DirectQuery model on Azure SQL with RLS by bank and reviewer role |
| LLM reads document text that could contain instructions | Inputs are public agreements and synthetic files; outputs are JSON and every value is quote-checked | Content safety / prompt shields; keep the "never produce a number" rule |
| Review queue ages are simulated at load | Labeled on the dashboard | Real ages from intake |
| Synthetic loan files are cleaner than real ones | Stated in every result | Pilot on a real (redacted) file set before any claim |
