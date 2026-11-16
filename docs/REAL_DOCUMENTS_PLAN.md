# Plan: training on real loan files (bounding-box labels) and moving the documents safely

A plan, not yet carried out. It assumes real scanned or digital loan files (PDF or TIFF) become available from a data
owner, and reuses this repo's pipeline: today the training labels are generated from synthetic ground truth
(`src/train_neural.py labels`); with real files, people draw the labels and everything downstream stays the same.
Read with [ETHICS_REVIEW.md](ETHICS_REVIEW.md): its checklist (data agreement, privacy and security review) comes first.

## 0. Principle: bring the code to the data

Real loan files hold borrowers' personal data. The preferred setup is to **deploy this repo into the data owner's own
Azure tenant** (`infra/up.sh`, `infra/ask_up.sh` and the Power Platform solution are all code), so documents never
leave their environment. Only models, metrics and code move. The rest of this plan assumes that, and section 2 covers
the case where files must be transferred.

## 1. Phases

| Phase | What | Exit criterion |
|---|---|---|
| 1. Agreements | data agreement, privacy impact assessment, security review (ethics review §8) | signed off |
| 2. Intake | receive files, inventory, hash, malware scan (§2) | manifest reconciles 100% to the sender's list |
| 3. Prepare | normalize, OCR (Layout), split into documents (§3) | every page has OCR; document boundaries labelled on a sample |
| 4. Label | bounding-box labels in Document Intelligence Studio, model-assisted (§4) | target counts per document type and bank; agreement checks pass |
| 5. Train | classifier + custom neural extraction (§5) | trained on train banks only |
| 6. Evaluate | held-out banks, by field and by segment (§6) | meets the acceptance thresholds from the cost model |
| 7. Pilot | shadow mode: model runs, people do everything as before, outputs compared | silent-error rate measured on an audit sample |
| 8. Operate | new-bank burn-in at full review, then per-field thresholds | reviewed and re-approved on a schedule |

## 2. Sending and receiving the documents

**Never** by email, chat, consumer file sharing or removable media without encryption.

| Situation | How | Notes |
|---|---|---|
| Preferred: processing in the owner's tenant | the owner uploads into the `raw` container of their own deployment | nothing crosses organizations |
| Owner sends to a project environment, online | **AzCopy** or Storage Explorer with **Entra ID sign-in** into a storage account with shared keys disabled (already the case in `infra/main.bicep`), public network access off and a **private endpoint** or an IP allow-list | per-person accounts, no shared links; access expires after intake |
| Owner's process needs SFTP | **SFTP-enabled ADLS Gen2** with local users using SSH keys, scoped to one container, disabled after transfer | for systems that can only push SFTP |
| Terabytes, slow links | **Azure Data Box** (encrypted appliance shipped by Microsoft) | chain-of-custody paperwork |

For every transfer:
- **Manifest first**: the sender provides a list (file name, bank, loan number, page count, SHA-256). Intake recomputes
  hashes and reconciles; mismatches are resolved before any processing.
- **Encryption**: TLS in transit; at rest with a **customer-managed key** in Key Vault, so the owner can revoke access.
- **Malware scanning** on upload (Microsoft Defender for Storage), quarantine container for anything flagged.
- **Immutable raw zone**: the `raw` container gets a time-based immutability (WORM) policy; originals are evidence and
  are never edited. All processing writes elsewhere (`docintel`, `pages`, `curated`).
- **Chain of custody**: who sent, who received, when, hashes, stored in the ops tables (`ops.run_log`) and the
  storage account's diagnostic logs (Log Analytics).
- **Retention and deletion**: a lifecycle policy deletes working copies after the engagement; originals follow the
  owner's records schedule; deletion is logged.

## 3. Preparing the pages

1. **Normalize**: TIFF to PDF, fix rotation, split files over the service's size or page limits (`src/di_chunk.py`
   already chunks long PDFs).
2. **OCR with Document Intelligence Layout** on every page (`src/run_layout.py`): text, words with polygons, tables,
   selection marks. This becomes the `.ocr.json` the labeling tool and the trainer use. About $10 per 1,000 pages.
3. **Document boundaries**: a reviewer marks where each document starts and ends in a sample of loan files (note,
   mortgage, appraisal, servicing history, comment log...). These train the classifier (`src/classifier.py`).
4. **Development copies**: if any copy must be used outside production (e.g. by a wider team), redact PII first:
   detect with Azure AI Language PII detection on the OCR text, map the entities back to word polygons, and burn
   black boxes into the page images; keep the mapping only in production.

## 4. Labeling with bounding boxes

**Tool:** Document Intelligence Studio, custom extraction project pointed at the training container. A labeler selects
the words of each field's value on the page; Studio writes, per document:

| File | Content |
|---|---|
| `<doc>.pdf.labels.json` | each field → its value text, page and **boundingBoxes** (word polygons normalized to the page, 0-1) |
| `<doc>.pdf.ocr.json` | the Layout result the boxes refer to |
| `fields.json` | the field list and types for the project (from `schema/loan_fields.yaml`) |

These are exactly the files `src/train_neural.py labels` already writes for synthetic documents (the `boundingBoxes`
are built from Layout word polygons the same way), so training, analysis and evaluation code don't change.

**Model-assisted labeling** (to cut labeling time):
1. Pre-label with the current model (v3) and with the **bank's own loan schedule**: for fields the schedule has
   (balance, rate, maturity), find the printed value on the page the same way `train_neural.py`'s `locate()` does
   (the shortest run of words that normalizes to the value, nearest its label).
2. A labeler confirms, moves or redraws each box in Studio. Pre-labels are a starting point, never accepted unseen.
3. Track how often pre-labels were wrong: it's an early accuracy estimate for the new bank.

**Labeling guide** (written before labeling starts, one page per document type):
- which occurrence counts when a value appears more than once (e.g. the note's amount box, not the text paragraph);
- printed vs handwritten values; amended or struck-through values (label the final, note the amendment);
- units and formats: label exactly what's printed; normalization happens in code (`src/normalize.py`);
- tables (servicing history): label as a table field with rows, or the needed cells only;
- checkboxes and signatures as selection marks; "not present" is a label too (so missing isn't confused with missed).

**Quality control:**
- **Double-label 10-20%** of documents independently. Agreement: value matches after normalization, and box
  overlap (IoU) ≥ 0.5. Disagreements go to an adjudicator, and the guide is updated.
- Labelers see only the documents assigned to them; labeling happens inside the controlled environment.
- Label versions are kept (the container is versioned; a manifest records who labeled what, when).

**How many:** Document Intelligence custom neural models train from as few as 5 labeled documents per type, but this
project's own results say **variety beats volume**: three banks' layouts gave 87-99.5% at new banks, twenty more
varied banks gave 96-99.7%. So label **many banks lightly** (e.g. 10-20 documents per type per bank across 10+ banks)
rather than one bank heavily. Time: roughly 2-5 minutes per document with pre-labels.

## 5. Training

**Splits by bank, never at random.** Train banks, validation banks (choose thresholds), and **held-out banks scored
once** after everything is frozen, exactly as in RESULTS.md. Keep every document of a loan in the same split.

| Model | How | Where |
|---|---|---|
| Classifier (split loan files into documents) | Document Intelligence custom classifier on the boundary labels | `src/classifier.py` |
| Field extraction | Document Intelligence **custom neural** model built from the labeled container; the service reads it with its managed identity (no SAS tokens) | `src/train_neural.py build` |
| One model per document type, composed | a composed model routes each document to its type's extractor | DI composed models |
| Risk flags (text) | refit the TF-IDF model on real labeled comment logs and letters; keep the LLM + verbatim-quote check; the agreement rule decides auto-accept | `src/text_flags.py` |

**Optional comparison: open-source layout models** fine-tuned on the same boxes (convert `labels.json` to token tags
with boxes scaled 0-1000, train on GPU compute inside the tenant). Only worth it if Document Intelligence falls short
on a document type. Check model licenses first (LayoutLMv3's published weights, for example, are non-commercial).

## 6. Evaluation and acceptance

On held-out banks only, per field:
- recall and precision of the model alone;
- **after routing: silent errors and share reviewed** (the numbers that matter);
- **by segment**: scanned vs digital, handwriting, language, loan type, bank, region (ethics review §3);
- **audit sample**: a random 1-2% of auto-accepted values re-checked by a person, so the silent-error rate is
  measured, with a confidence bound.

Acceptance thresholds come from the cost model (ethics review §2): a field may be auto-accepted at a bank only when
the upper confidence bound of its silent-error rate is below C_review / C_error for that field. Until then, it goes to
review. Every new bank starts at full review (burn-in).

## 7. What changes in the repo

| Today (synthetic) | With real files |
|---|---|
| `src/synth/generate.py` makes loan files with planted truth | intake (§2) + manifest |
| `train_neural.py labels` writes labels from planted truth | labels come from Studio (people, model-assisted); the same files |
| `evaluate.py` compares with planted truth | compares with human labels on held-out banks + the audit sample |
| text flags graded against planted flags | graded against labeled real text |
| Loan Desk Q&A searches synthetic pages | same, plus PII rules enforced (answers never show IDs or addresses) and the red team re-run on real text |

## 8. Rough cost and effort (to refine with real volumes)

| Item | Estimate |
|---|---|
| Layout OCR | ~$10 per 1,000 pages |
| Labeling | 2-5 min per document with pre-labels; double-labeling adds 10-20% |
| Training | custom neural training and classifier builds: check the region's current quotas and prices |
| Extraction at run time | per page, as today (RESULTS.md has the measured cost per loan) |
| People | a labeling lead who writes the guide and adjudicates; labelers with access approval; a reviewer for the audit sample |
