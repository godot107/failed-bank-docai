# Ethics review and the cost of mistakes

A review of Receivership Loan File Desk (extraction, review app, loan view, Loan Desk Q&A, prescriptive queue, failure
mining) before anyone would use it on real loan files. It says who could be hurt, what each kind of mistake costs, what
the system already does about it, and what is still open. It is a self-review, not a legal opinion: anything marked
**[counsel]** needs a lawyer or compliance officer before real use.

*Today every loan file, borrower and bank in the app is synthetic; the FDIC records are public. Most of the harms below
only become real with real files, which is why this review comes before the plan in [REAL_DOCUMENTS_PLAN.md](REAL_DOCUMENTS_PLAN.md).*

## 1. Who is affected

| Who | What they rely on | How the system could hurt them |
|---|---|---|
| **Borrowers** of the failed bank | their loan record being right after the bank is gone | a wrong balance, rate or due date follows the loan to a buyer; a wrong "hardship" or "bankruptcy" flag changes how the loan is priced or serviced; their personal data is exposed |
| **Bidders / buyers** | the data room describing the loans accurately (sold without warranties) | they misprice a pool, or discount every pool for uncertainty |
| **The receiver and the Deposit Insurance Fund** | sales recovering as much as possible, fairly and fast | lower recoveries from mispriced or delayed sales; reputational harm from a data error or a leak |
| **Reviewers** | the queue and the agent pointing them at what matters | automation bias: trusting a confident machine; workload pushed onto them without the time to do it |
| **The public** | resolution being competent and even-handed | loss of trust if automation is seen to cut corners on people's loans |

## 2. The cost of mistakes

Not all mistakes cost the same, and the system's settings should follow the costs, not just accuracy.

| Mistake | Example | Who pays | Relative cost |
|---|---|---|---|
| **Silent wrong value** (auto-accepted, nobody looks) | balance or rate wrong in the data room | bidders misprice; borrower's record wrong; receiver's recovery | **highest**: nothing flags it, and it travels with the loan |
| Wrong value that a reviewer catches | the payment read as the principal, routed to review | reviewer time | low: minutes of work |
| Wrong value that a reviewer *accepts* | reviewer trusts the machine | as silent error | high: why the app shows the page and the bank's schedule side by side |
| **Missed risk flag** | a bankruptcy or litigation note not flagged | buyer, borrower (collection during a bankruptcy can break the law) **[counsel]** | high |
| False risk flag | "hardship" flagged on a loan without one | borrower (worse pricing or treatment), reviewer time | medium: flags are labelled as a model's reading and need confirmation |
| Wrong sale pool | a misread next-due date moves a delinquent loan to "performing" | bidders on that pool | medium-high: changes what a pool is |
| **Agent answer wrong** | Q&A misstates a figure or a rule | reviewer decisions built on it | medium: quotes and figures are checked, but explanations aren't |
| Prescriptive ranking wrong | a risky item ranked low | delayed catch | low-medium: every item is still reviewed; ranking only orders the work |
| Data leak | borrower PII leaves the controlled environment | borrowers, receiver | **highest**, and irreversible |

### Expected cost: why "0 silent errors" isn't the end of the argument

The final clean holdout had **0 wrong values in 197** with 2% of values reviewed. Zero in 197 doesn't mean zero: by
the rule of three the true silent-error rate is plausibly **up to ~1.5%** (3/197). On a real failure with 2,000 loans
x 10 fields = 20,000 values, that bound allows up to ~300 wrong values reaching the data room unseen.

Per value, with a review threshold set:

    expected cost = P(silent error) x C_error  +  P(sent to review) x C_review

| Parameter | Value used here | Source |
|---|---|---|
| C_review | 4 minutes of a reviewer (≈ $5 at $75/hour loaded) | assumption, same as the release plan |
| P(sent to review) | 2% (familiar format) to 13.5% (new bank) | measured on held-out banks (RESULTS.md) |
| P(silent error) | 0 observed; ≤ 1.5% plausible | 0/197, rule of three |
| C_error, balance or rate field | a share of the loan's value mispriced; for a $380k loan even a 1% pricing effect is ~$3,800 | assumption, illustrative |
| C_error, flag missed (bankruptcy) | legal exposure, not a pricing number **[counsel]** | — |

Reviewing a value costs about $5; a silent error on a typical loan's balance plausibly costs hundreds to thousands.
**When a mistake costs ~1,000 times the check, reviewing is worth it whenever the chance of an error is above ~0.1%**,
which is below what 197 clean values can rule out. Consequences for the settings:

1. **Start every new bank at full review** (already the recommendation), and lower review only as that bank's own
   reviewed sample shows the error rate is below the break-even (C_review / C_error), with a confidence bound, not
   a point estimate.
2. **Set thresholds per field by consequence**, not one confidence cutoff: balance, rate, next-due date (pool) and
   bankruptcy/litigation flags at the strictest; dates of minor documents looser.
3. **Rank review by expected dollars at risk** (the prescriptive queue already does: chance x balance), so a limited
   review budget goes where mistakes cost most.
4. **Audit a random sample of auto-accepted values** at every bank (e.g. 1-2%), so the silent-error rate is measured,
   not assumed. Without it, "0 silent errors" can never be confirmed in production because nobody looks.

## 3. Fairness

The model treats documents, not people, but document quality isn't evenly spread.

- **Risk:** older scans, handwriting, faxed pages and non-English letters are read worse. These are more common at some
  banks, regions and borrower groups (e.g. small rural banks, minority depository institutions, Spanish-language
  correspondence), so errors or missed flags could concentrate there.
- **What to measure on real files:** accuracy, review rate and flag recall **by bank, document quality (scanned vs
  digital, handwriting), language, loan type and region**. A gap is a reason to route that segment to full review,
  not to accept a lower standard.
- **Hardship and modification flags** describe a borrower's circumstances. They should inform how a loan is described
  and serviced lawfully, never become a basis for adverse treatment beyond what the law and the receiver's policy allow
  **[counsel: fair lending, consumer protection]**.
- **The failure mining** uses public, aggregated bank data and makes no statements about individuals. Its findings are
  associations among failed banks, labelled as such.

## 4. Privacy and security

Real loan files are full of personal data: names, addresses, Social Security numbers, income, bank statements.

- **Minimize**: extract only the fields the loan tape needs; don't store full text longer than the review requires.
- **Keep it in the controlled environment**: process in the data owner's own cloud tenant (see the documents plan);
  no copies on laptops, email or chat.
- **Redact for development**: any copy used outside production gets PII redacted first (section 3 of the plan).
- **The agent**: answers don't include identification numbers, addresses or phone numbers (rule 10, red-team tested);
  questions and answers aren't kept with a person's name; logs carry no question text.
- **Access**: Entra sign-in, least privilege (the agent's database user reads only what it needs), every decision
  logged with who and when.
- Applicable rules (e.g. GLBA safeguards, the data owner's records and security policies, federal requirements where
  they apply) **[counsel / security officer]**.

## 5. Transparency and human oversight

- People decide: release, hold, bids and legal positions stay with reviewers; the agent says so and won't advise on
  them (red-team tested).
- Every value shows where it came from (document, page, the bank's schedule); every agent quote and figure is checked
  against the data; risk flags are labelled "a model's reading".
- Nothing is overwritten: model value, reviewer correction, who and when are all kept (system-versioned tables).
- **Automation bias** is the main residual oversight risk: a reviewer who accepts whatever is shown. Mitigations to add
  with real files: occasional known-wrong test items in the queue to measure whether reviewers catch them; showing
  confidence plainly; never pre-filling the correction box with the machine's value for high-consequence fields.

## 6. Security of the AI layer

Covered in [LOAN_DESK_QA.md](LOAN_DESK_QA.md) §2: document text from the failed bank is treated as untrusted and
screened for planted instructions; red team 2/18 attacks worked before the guardrails, 0/18 after. Residual: new attack
styles; the red team should be re-run on every model or instruction change and on a sample of real documents.

## 7. Honesty about what was tested

- Accuracy figures come from **synthetic** loan files; real files are messier (handwriting, stains, 400-page files).
  No accuracy claim should be made about real files until the pilot in the documents plan has measured it.
- Two of the held-out banks informed model choices; only the last one (B9) is clean, and it is one bank.
- Costs in section 2 are illustrative assumptions, shown so they can be replaced with the receiver's own numbers.

## 8. Before real use: checklist

| | Item | Owner |
|---|---|---|
| ☐ | Data processing agreement and approval to process the files in the chosen environment | data owner, counsel |
| ☐ | Privacy impact assessment | privacy officer |
| ☐ | Security review of the environment, identities and logging | security officer |
| ☐ | Per-field thresholds and review policy set from the cost table with the receiver's numbers | business owner |
| ☐ | Pilot on real files measured: accuracy, silent errors (audit sample), fairness segments (section 3) | project team |
| ☐ | Reviewer training, including automation bias | business owner |
| ☐ | Red team re-run on real documents; incident response for a data or model error | project team, security |
| ☐ | Sign-off and a date to review again | business owner |
