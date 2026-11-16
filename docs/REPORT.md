# Reading a failed bank's paperwork, fast: what document AI can and can't be trusted with

*A personal research project on bank failures, from the 2008 mortgage bubble to the 2023 bank runs.
Loan files are synthetic (fictional banks and people); the public data and agreements are real.
Not affiliated with or endorsed by any agency or organization.*

## Why this matters

When a bank fails, the FDIC steps in as **receiver**. It has to sell the bank, usually to a healthy one, and
manage whatever the buyer doesn't take. Buyers price a failed bank by its loans, and the loans live in
**loan files**: scanned promissory notes, mortgages, appraisals, payment histories, collector notes. Every
bank formats them differently.

In 2009 and 2010, banks failed at a pace of two to three a week (148 and 157 a year). In March 2023, Silicon
Valley Bank lost depositors so fast that preparation measured in months shrank to days. Either way, the
question is the same: **how quickly can a stack of unfamiliar paperwork become numbers people can trust?**

## What I built

1. **A document AI pipeline** (Azure AI Document Intelligence) that splits a scanned loan file into its
   documents, reads the key numbers (loan amount, rate, payment, maturity, balance, appraised value), and
   flags problem loans from the free text (bankruptcy, litigation, hardship, underwriting exceptions).
2. **Safety checks** that decide what a person needs to look at: model confidence, validity rules, and a
   cross-check against the bank's own loan schedule.
3. **A review app** (Power Apps) where a person confirms or corrects what the machine wasn't sure about, and
   **automations** (Power Automate) that apply those decisions to the official record, keep every past value,
   and escalate anything waiting too long.
4. **Dashboards** (Power BI) for "can we trust the data?", "is review keeping up?", and "what is it costing?".

Real loan files are confidential, so I generated synthetic ones for about 50 fictional banks. Each copies a
real failed bank's mix of loans (from its public financial reports), and each has its own layout, wording,
date and money formats, and note-writing style. **Every test uses whole banks the model has never seen.**

## What I found

**1. Hand-written rules don't survive a new bank.** A parser written from three banks' paperwork read 84% of
their values correctly and **0% at the next bank**, which wrote "Sum Borrowed" where the others wrote
"Principal Amount". It failed safely (blank, not wrong), but a person would have typed everything.

**2. A trained model generalizes, once it has seen enough variety.** Trained on three banks, the model read
87–99.5% of values at new banks. Trained on twenty more randomly styled banks, it read **96–99.7%**. But how
the extra banks were made mattered: a version that also randomly deleted fields and added decoy numbers got
*worse* (73% at one bank), because it learned "grab the nearest dollar amount".

**3. The safety checks came from failures, not from planning.** Each new held-out bank broke something:
- one listed fields in reverse order and the model put the monthly payment in the loan-amount field
  (caught by checking against the bank's schedule, and by a rule that a payment must be a small fraction of
  the loan);
- one printed dates day-first (`06.04.2008` = 6 April). The model read them perfectly; **my own code** turned
  them into June 4th, and nothing flagged it. The fix: learn each bank's date order from dates that can only
  be read one way, and send ambiguous dates to a person until it's known.

With those checks, every bank tested ended with **0 wrong values slipping through**, and a person reviewing
**about 2%** of values (12% at the hardest bank).

**4. For free text, combine a careful model with a thorough one.** Simple text models were never wrong but
missed half the flags at a new bank. An LLM caught every flag but raised false alarms. Accepting what both
agree on and sending the LLM's extra flags to a person gave **zero wrong auto-accepts and every real flag
found**, with about half the flags reviewed.

**5. On real documents, it reads the deal terms.** I ran the pipeline on 22 real purchase-and-assumption
agreements from 2009–2011 failures (60–200 scanned pages each) and checked it against the FDIC's published
bid summaries. Reading only 3% of the pages, it got the buyer and date right 22/22 and made **no wrong
readings** of the premium, discount or loss-sharing terms; its misses were blanks. Cost: about $1.

## What I'd tell someone deploying this

- **Start every new bank at full human review**, and lower it only as accuracy is proven on that bank.
- **Measure silent errors, not accuracy.** A wrong number nobody looks at is the expensive one.
- **Reconcile against an independent source** (the bank's own schedule) whenever one exists; it caught most
  of the model's mistakes.
- **Keep every version.** The review app never overwrites: the model's value, the reviewer's correction, who
  made it and when are all kept.

## Limits
Synthetic loan files are cleaner than real ones (no handwriting, coffee stains, or 400-page files). Some
risk flags (fraud, property damage) had only a handful of examples. Two holdout banks were used to choose
between models, so a fresh bank is needed for a final, unbiased number. The real-agreement test covers 22
deals, not the whole wave.

*Numbers and methods: [RESULTS.md](RESULTS.md). Code and setup: [README](../README.md).*
