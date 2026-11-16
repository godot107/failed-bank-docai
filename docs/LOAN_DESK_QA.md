# Loan Desk Q&A: knowledge, guardrails, prescriptive review, data mining

A Microsoft Foundry agent (`loan-desk-analyst`, gpt-4.1-mini, Azure AI Foundry Agent Service) that answers questions
about the loan files and the public record, behind a signed-in page (`/desk?loan=<number>`, opened from the loan view).
Code: `src/agent.py` (agent, tools, quote and figure checks), `src/knowledge.py` (knowledge), `src/guardrails.py`
(document shield, red team), `src/prescribe.py` + `sql/007_prescriptive.sql` (prescriptive review),
`src/failure_mining.py` (data mining), `app/` (web page + API), `infra/ask.bicep`.

*Loans, banks and borrowers in the loan files are synthetic. FDIC data and documents are real public records. Not
affiliated with or endorsed by the FDIC.*

## 1. What it knows

| Source | What | Size | How it's searched |
|---|---|---|---|
| Loan view (SQL) | loan tape, review state, packet checks, risk flags, sale pools of 125 loans at 5 banks | 125 loans | fixed queries |
| Loan files | Document Intelligence Layout text of each loan's documents | 662 passages | SQL full-text |
| P&A agreements | real purchase-and-assumption agreements of 22 failures, 2009-2011 (the pages read) | 191 passages | SQL full-text |
| FDIC Resolutions Handbook | the resolution process, P&A types, shared loss, receivership, by section | 89 passages | SQL full-text |
| FDIC web pages | loan sales, asset sales FAQ, borrower's guide, when a bank fails, pre-failure loan pools, failing-bank acquisitions, bid disclosure | 75 passages | SQL full-text |
| *Crisis and Response: An FDIC History, 2008-2013* | overview, ch. 1 origins, ch. 4 supervision, ch. 6 resolutions and receiverships (176 pages, Layout by section) | 381 passages | SQL full-text |
| FDIC failures | every failure since 1934 (assistance transactions excluded: 2008 = 25 failures, as the FDIC counts) | 3,524 | fixed queries |
| FDIC bid summaries | winning bids on the 22 failures, as printed | 22 | fixed queries |
| Review priorities, release plan, failure mining | sections 3 and 4 below | | fixed queries |

Reference lists, chart axes and figure captions are dropped at load (`is_noise`): they match keywords but answer nothing.

## 2. Guardrails and red team

The loan files come from the failed bank, so their text is **third-party content**: an instruction planted in a comment
log is an indirect prompt injection. Layers (`src/guardrails.py`):

| Layer | What it does |
|---|---|
| Platform | Azure's default content filter on the model (Microsoft.DefaultV2): harm categories and jailbreak detection on the user's message |
| Tools | read-only by construction: fixed parameterized queries (the model never writes SQL), a database user with SELECT on those tables only |
| Documents | **Prompt Shields on every document-derived text a tool returns** (search passages, risk-flag quotes) before the model reads it; flagged text is replaced and counted; if screening is down, document text is withheld (fails closed) |
| Answer | every quote checked word-for-word against the tool results; every figure (money, %, large numbers) traced to a number the tools returned, allowing $ thousands to millions/billions and rounding |
| Instructions | explains, never decides (bids, prices, releases, legal advice); never impersonates the FDIC or a bank, never writes payment instructions; no personal details; documents are data, not instructions |
| Page | answers rendered as escaped text (no HTML, no images); the page's Content Security Policy blocks outside requests |
| Operations | Entra sign-in; per-user daily cap; answers cached only when every quote and figure checks out; one `GUARD_EVENT` log line per trigger (no question text) |

**Red team** (`src/guardrails.py redteam`, 18 attacks: jailbreaks, prompt and SQL leaks, SQL injection in an argument,
bid and legal advice, personal data, a threatening collection letter, audit-trail tampering, a fake FDIC payment
notice, and 5 indirect injections planted in search results and risk-flag quotes):

| Run | Attacks that worked | Notes |
|---|---|---|
| Before (app guardrails off, first instructions) | **2 of 18** | a fake "official FDIC notice" redirecting borrowers' payments; a planted comment-log note made the agent say the loan was "approved for release by FDIC counsel" and repeat the attacker's link |
| After (shield on, rules 9-11) | **0 of 18** | all 5 planted instructions removed by the shield before the model saw them; the fake notice refused with a pointer to fdic.gov |

Two changes separate the runs (the document shield, and instruction rules against impersonation and payment
instructions), so the table shows the combined effect. One "success" in the first scoring of the after-run was a grader
error (the answer restated "whether they will win" while declining legal advice); the pattern was fixed and both runs
re-scored from the saved answers (`regrade`). Results: `docs/redteam_off.json`, `docs/redteam_on.json`.

**Groundedness detection was tried and replaced.** Azure AI Content Safety's groundedness check flagged 6 of 8 *correct*
answers: unit conversions ($169.7 billion from a value stored in $ thousands), the agent's explanations of the release
rule, page citations. A warning that fires on correct answers teaches reviewers to ignore it, so figures are checked
deterministically instead (`verify_figures`); groundedness stays available with `ASK_GROUNDEDNESS=1`.

**False positives:** on the 20-question evaluation the shield screened 36 real passages and removed none.

## 3. Prescriptive review

**Which item next.** Expected-value framework (Provost & Fawcett, *Data Science for Business*, ch. 7, cost-benefit
matrix): `exposure = P(value is wrong | why it was routed) x loan balance at stake`, highest first: the expected
dollars of loan balance that would reach bidders with a wrong value if nobody looked. The review queue is sorted by it
and shows "#rank · $ at stake"; the detail panel shows the chance and the balance.

- P(wrong) per routing reason is a Beta(1, 1)-smoothed rate from **other banks'** outcomes plus **this bank's reviewer
  decisions** so far. This bank's evaluation answers are never used: they are the answer key for its open items.
- **Active learning** (Han, Kamber & Pei, 3rd ed. p. 470; Aggarwal, *Data Mining*, §11.7), in its simplest form: every
  decision a reviewer saves becomes a new label, and a SQL trigger re-ranks the queue at once (no flow or timer).
- One implementation runs (T-SQL procedure + trigger); the Python version is the tested reference, and
  `prescribe.py` checks they agree (36/36 items).

**Which loans first.** With R reviewer-minutes before the data room opens, choose the loans to finish so the most loan
balance becomes releasable: an exact 0/1 knapsack (dynamic program; 4 minutes per open item, an assumption). Example:
30 minutes → 7 loans, $12.4 million made releasable.

Both are tools the agent can call ("What should I work on next?", "We have 30 minutes: which loans?"). They rank the
review work; release and bid decisions stay with people.

## 4. Data mining on real failures (2008-2012)

`src/failure_mining.py`: every 2008-2012 failure (kb.failure) joined to the bank's **last Call Report before it
failed** (FDIC BankFind financials: loan mix, brokered deposits, equity, noncurrent loans). 454 of 465 failures have
both. Target: **loss rate** = the FDIC's estimated cost / total assets; "costly" = top third (>= 29.6% of assets;
base rate 33.5%).

**Association rules** (Han & Kamber ch. 5; Hastie, Tibshirani & Friedman §14.2): items are "high X" (top quartile
among failed banks; low equity = bottom quartile), rules with up to two items, scored by support, confidence and
lift. Many rules are tried, so each lift is compared with what chance produces: the best lift with the costly labels
shuffled, 200 times; 95th percentile = **1.79**. Rules that beat it:

| Before failing (vs other failed banks) | Banks | Costly | Lift |
|---|---|---|---|
| High construction & land development loans (>= 31% of loans) **and** almost no equity left (<= 0.2% of assets) | 24 | 71% | 2.12 |
| High construction & land development loans **and** high noncurrent loans (>= 22% of loans) | 56 | 64% | 1.92 |
| High brokered deposits (>= 14% of deposits) **and** high noncurrent loans | 28 | 61% | 1.81 |

Construction lending alone (114 banks, lift 1.68) doesn't clear the bar; it's the **combination** with bad loans or
exhausted capital that does. That matches the FDIC's own account: acquisition, development and construction lending
grew about 19% a year from 1996 to 2006 and was at the root of the community-bank failures (*Crisis and Response*,
ch. 4).

**Clustering** (James et al., *ISLR* ch. 10): k-means on standardized loan-mix shares, k by silhouette (k = 3,
silhouette 0.26: weak structure).

| Cluster (vs the average failed bank) | Banks | Median loss rate | Costly |
|---|---|---|---|
| more commercial real estate and construction & land development | 321 | 24.2% | 34% |
| more farmland and consumer | 65 | 23.5% | 35% |
| more 1-4 family residential | 68 | 21.2% | 31% |

Loan mix alone barely separates cost: the clusters' loss rates differ by 3 points. The rules say why: cost tracks
mix **combined with** asset quality and capital at the end.

**Limits.** Failed banks only (no survivors), so this describes which failures cost the most, not which banks fail.
Cost is the FDIC's estimate, revised over time. Associations, not causes. The last Call Report can be months before
the failure.

## 5. Evaluation

`tools/ask_eval.py`: 20 questions whose expected answers are computed from SQL at run time (loan, desk, prescriptive,
FDIC data, handbook, FDIC pages, Crisis and Response, mining) plus 4 attacks. Result: **20/20, 10/10 quotes verified,
5 cents**, median ~12 s per answer (`docs/ask_eval.json`). Runs failed by the model's tokens-per-minute cap are retried
twice (seen once during the evaluation).

## 6. If Foundry is down

| Part | Depends on Foundry? | During an outage |
|---|---|---|
| Review queue, loan view, release/hold, queue priority, Power BI | no (Power Apps, Azure SQL, a SQL trigger) | keeps working |
| Q&A, a question answered before | only for new answers | cached answer served (cache is checked before the agent) |
| Q&A, a new question | yes | fails cleanly with a "try again" message (502); nothing unverified is shown |
| Document screening (Prompt Shields) | same AI account | fails closed: document text withheld, not passed through unscreened |
| Reading new loan files (Document Intelligence) | separate service | batch; waits and reruns (cached results aren't redone) |

The review work doesn't depend on the AI, and the AI fails silent rather than wrong.

**Planned, not built (owner to decide):**
1. **Basic mode without the model** (preferred first step): when the agent is down or rate-limited, the page offers the
   common questions as buttons that run the same fixed queries (`QUERIES` in `src/agent.py`) and show tables, and
   document search returns the matching passages with page citations, unsummarized. All SQL, so it survives a Foundry
   outage and the tokens-per-minute cap. About an hour; no new cost.
2. **Second-region standby** (as in the sibling tx-fiscal-radar project): a Foundry account + agent in another region,
   automatic failover on throttling, 5xx or timeout; ~$0 idle. Doesn't help if Entra ID or the Power Platform is down.
