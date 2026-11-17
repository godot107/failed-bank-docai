# Experiment design: person-time saved by Document Intelligence vs keying by hand

Status: **designed, not run.** Nothing below is built beyond what already exists (the `/manual` timed keying page
and the review app's timing). This document is the plan, written before any data, so the analysis can't be fitted
to the results.

## Question

How many person-minutes does it take to get one loan's values onto the loan tape **correctly**: keyed into a web
form by hand, or read by Document Intelligence and checked by a person?

"Correctly" matters. Manual keying has its own error rate (single keying is commonly cited at 0.5–2% of fields),
which is why operations add a second pass. The comparison is at equal final quality, or it reports time and error
side by side, never time alone.

## Hypotheses

- **H1 (time):** person-seconds per document are at least 3× lower with AI + review than with single manual keying.
- **H2 (quality):** the final field error rate with AI + review is no worse than with manual key + verify.
  At this sample size H2 can only be checked loosely (see Sample size); it is reported, not claimed.

## Arms

| Arm | What the person does | Exists today? |
|---|---|---|
| **A. Manual, single key** | Types every field from the page into an empty form | Yes: `/manual` (timed, scored against the answer key) |
| **B. Manual, key + verify** | A second person checks A's entries against the page and fixes them | No |
| **C. AI + review (review app)** | Works only the values routed to review; the rest are accepted | Yes: Power Apps review app stamps `opened_at` / `seconds_on_item` |
| **C′. AI, prefilled form** | The same form as A, prefilled with the model's values, routed fields highlighted; confirm or fix | No |

**Primary comparison: A vs C′.** Same screen, same timer, same person; the only difference is whether the boxes
start empty or prefilled. C (the real review app) is a secondary check, because it also changes the interface.
B is needed to compare at equal quality.

## Biases in the current setup, and the fix

| Problem | Why it matters | Fix |
|---|---|---|
| `/manual` serves the 28 documents that have review items | Those are the documents the model found hard; manual keying is timed on the hardest pages | Random sample of **all** documents, stratified by bank style (S3–S8) and document type |
| `/manual` shows the right page | Real keyers must first find the note in a 20–100-page loan file; the classifier does that for free | Run arm A two ways: page given, and whole file; report both |
| The same person could key a document and later review it | Memory of the answers makes the second arm look fast | Each person gets different documents per arm; document sets rotate across people (Latin square); arm order alternates |
| The builder knows the documents and the app | Faster in both arms, and not like a new hire | Report the builder's run separately from other participants |

## Protocol

1. **Participants:** 4–6 people new to the documents, plus the builder reported separately. 2 practice documents
   each in every arm, not counted.
2. **Documents:** about 12 per person per arm, drawn at random within strata, none used for model training.
3. **Sessions:** 30 minutes or less; one arm per session; order alternated (half the people start with A, half
   with C′).
4. **Recorded per document:** participant (pseudonymous id), arm, seconds on the document, every keyed or
   confirmed value, and each value scored against the planted answer key (`eval.field_truth`; numbers right
   within half a cent or 0.005 points, dates exact, as in `src/turnaround.py`).
5. **Arm B:** a different participant verifies a random half of arm A's documents; time and fixes recorded.

## Metrics and analysis

| Metric | How |
|---|---|
| Person-seconds per document and per field | Median by arm; 95% bootstrap interval, resampling participants then documents (paired by person) |
| Ratio A ÷ C′ | Same bootstrap; H1 holds if the interval's lower end is ≥ 3 |
| Final field error rate | Errors ÷ fields by arm, with a Wilson interval; zero errors reported as "below 3/n" (rule of three) |
| Errors by field and error type | Transposed / dropped digits, wrong year, day-month swap, payment read as principal |
| **Person-hours per 1,000 loans at equal quality** | Manual = (A + B) per loan × 1,000. AI = review share × seconds per review item × 1,000, plus 100% review of each new bank's first loans (burn-in), plus model setup (labeling and training hours) |
| **Break-even volume** | Loans at which AI setup time is repaid by the per-loan savings |

The AI arm's review share and silent-error rate come from the routing results already measured (`docs/RESULTS.md`);
the experiment supplies the human seconds.

No outliers are dropped. A document abandoned midway is recorded as abandoned and reported.

## Sample size

- **Time:** with about 50 documents per arm, a 3× difference in medians is detectable with a wide margin; timing
  data is skewed, hence medians and bootstrap intervals.
- **Errors:** with about 400 keyed fields per arm and a 1% error rate, expect about 4 errors: enough to report a
  rate with a wide interval, not enough to show one arm is more accurate than the other. Showing a difference of
  1% vs 0.2% would need about 1,500 fields per arm (80% power, 5% significance).

## Second question: elapsed time at scale

Person-minutes per loan is only half the benefit. The other half is **volume**: a failed bank hands over thousands
of loan files at once, and the work is due on a fixed schedule. A person keys one page at a time; the pipeline
reads hundreds of pages while nobody waits, and finds the right pages in each file on its own.

Measured machine throughput in this project (`samples/synthetic/ops/runs.jsonl`; one script, calls one after
another, no parallelism, no throttling seen):

| Stage | Pages | Pages per minute |
|---|---:|---:|
| Layout (text of every page) | 3,349 | 78 |
| Classifier (which document is on which page) | 845 | 60 |
| Custom extraction (the note, servicing history, appraisal) | 622 | 33 |

These are a floor: running calls in parallel, up to the service's rate limit, multiplies them. The machine side
also runs overnight and on weekends.

What the experiment adds is the human side. For a receivership of N loan files of P pages each, with k people:

- **Manual:** elapsed days = N × (seconds to find the pages + seconds to key them + seconds to verify) ÷ (k × working seconds per day).
- **AI:** elapsed days = max(machine time for N × P pages, N × review share × seconds per review item ÷ (k × working seconds per day)),
  plus burn-in for each new bank (100% review of its first loans).

The report gives both formulas filled in for N = 1,000 and 10,000 files, with the human seconds from the
experiment and the machine rates above. The question it answers: **how many people does each approach need to finish
on time**, which is the decision a receivership manager actually makes.

## What it costs

$0 in Azure: the documents are already extracted and the model's values are stored. To run it would take about
1–2 hours of build (C′, verify mode, random stratified queue, arm assignment) and about 1 hour per participant.

## Limits

- Synthetic, clean, mostly one-page documents. Real scans (skew, handwriting, stamps) take longer to key and longer
  to review. The **ratio** between arms is more trustworthy than the absolute seconds.
- Participants are not trained loan-data keyers; professionals key faster, which narrows the gap.
- Only the eight loan-tape fields are measured. Text risk flags (comment logs, borrower letters) are not keyed in
  this design; reading them by hand is slower still, so leaving them out favors manual keying.
