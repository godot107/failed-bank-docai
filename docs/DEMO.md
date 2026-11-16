# Demo talk track

Two versions: a **5-minute screen demo** (recording or live share) and a **90-second verbal version** (no screen).
Everything shown is synthetic loan files from fictional banks, plus public FDIC data and agreements.

## Before you start (10 minutes ahead)

0. **Turn on the flow "Apply review decisions"** (make.powerautomate.com → My flows → Turn on) about 15 minutes
   before, and **turn it off again afterwards**. The two flows are kept off between demos: on a 15-minute schedule
   they keep the free-offer database from ever pausing, which uses up its monthly free compute in under two days.
1. **Wake the database** (the free-offer Azure SQL pauses after 15 minutes idle; the first query takes ~1 minute):
   `.venv/bin/python tools/run_sql.py --query "SELECT COUNT(*) FROM review.item WHERE status = 'open'"`
2. **Open four tabs:**
   - the review app (make.powerapps.com → Apps → *Loan File Desk Review* → Play),
   - Power BI report *Loan File Desk*, page "Can we trust the loan tape?",
   - Power Automate → My flows → *Apply review decisions* (details page: shows On + run history),
   - `docs/img/` folder or the GitHub README (backup if anything won't load).
3. The queue is sorted by expected $ at risk; the demo item `B4-0001-PN Monthly payment` is about #12 (scroll a little,
   or open it from the loan list) (the model read the loan amount, 1,051,000, as the monthly
   payment; the page shows `PMT AMT 21,436.46`). Check it's still open with the query in step 1 (list the first items).
4. Close Power Apps Studio if it's open (it locks the app). If the player shows "You're using an old version of
   this app", press **Refresh** before you start.
5. **Warm up Loan Desk Q&A:** open https://func-fbd-mdbcct.azurewebsites.net/desk?loan=231370616 in the same browser
   (sign in with the work account once, so the button opens straight in during the demo) and ask both demo questions.
   The first answer after a quiet spell takes 30-60 s while the app starts; after that a new answer takes ~8-12 s and
   a repeated one ~1 s. Correcting the payment during the demo changes the data, so the release question is answered
   fresh (~10 s) and now says the loan is ready: that's the point.

## 5-minute screen demo

**0:00 – The problem (30 s, README or nothing on screen)**
> "When a bank fails, the FDIC as receiver has to sell it fast, and buyers price it by its loans. The loan data is
> locked in scanned files: notes, mortgages, appraisals, payment histories, collector notes, and every bank formats
> them differently. In 2009–2010 that happened two or three times a week; with Silicon Valley Bank in 2023, months of
> preparation shrank to days. So the question I built this around is: how fast can unfamiliar paperwork become
> numbers people can trust?"

**0:30 – The headline result (45 s, Power BI page 1)**
> "These are banks the model never saw in training, each with its own layout and wording. It reads 96 to almost 100
> percent of values correctly. But the number I watch is this one: *silent errors*, wrong values that nobody looks
> at. It's zero, because anything the system isn't sure about goes to a person, about 2 percent of values at most
> banks."
> Point at: Recall card → Silent errors card → the per-bank table.

**1:15 – Why it's zero (45 s, still page 1, or say it)**
> "A value is only auto-accepted if the model is confident, it passes validity rules, and it matches the bank's own
> loan schedule. None of those checks were designed up front; each one exists because a new bank broke something.
> One bank printed dates day-first. The model read them perfectly; my code misread them, and nothing flagged it. Now
> the system learns each bank's date format from dates that can only be read one way."

**2:00 – The review app (45 s, Power Apps)**
> "This is what the person sees. The page, what the model read, its confidence, why it was flagged, and the bank's
> schedule. Here the model swapped two numbers: it put the loan amount, $1,051,000, in the monthly-payment field.
> A rule caught it: a monthly payment can't be the whole loan. On the page, the payment is $21,436.46."
> Type `21436.46` → **Save Correction**.
> "That's it for the reviewer. Nothing is overwritten: the model's value stays in history, with who changed it and
> when."

**2:45 – The whole loan (45 s, Loan view ›)**
> "A receiver doesn't sell fields, it sells loans, in pools of similar loans. So this is the whole file: every field
> with the document and page it came from and the bank's own schedule next to it, the checks that compare documents
> with each other, and the risk flags from the comment log, each with the quote it came from. This loan goes in the
> commercial and industrial, non-performing pool: 347 days past due at closing. And the borrower asked for a payoff:
> they're refinancing, which changes what a buyer would pay."
> Point at: payment row now "Corrected by reviewer" → the packet check → the two payoff quotes → "Ready" →
> **Release to data room** (or *Hold* with a note).
> "Release stays disabled while anything is unresolved, and every decision is kept with who and when."

**3:30 – Ask the agent (40 s, Ask about this loan ›)**
> "Reviewers also have questions the screen doesn't answer. This button opens Loan Desk Q&A on this loan: a Foundry
> agent that can only run fixed read-only queries and search the documents."
> Click **Ask about this loan ›** → **Ask** (the first suggestion: "Why can't loan 231370616 be released yet?").
> "It names what's blocking release, and every quote is checked word-for-word against what it read: that's the green
> badge." Then click the FDIC chip **"What is a shared-loss purchase and assumption…"** → **Ask**.
> "It also knows the real record: every bank failure since 1934, 22 real deals, and the FDIC's Resolutions Handbook,
> cited by page. It explains; it won't tell you what to bid or whether to release."

**4:10 – Automation (25 s, Power Automate)**
> "Every 15 minutes this flow applies decisions to the official record: decision log, a new version of the value,
> an audit event. A second flow escalates anything past its 48-hour deadline. Both deploy with the app as one solution."
> Point at: Status On → run history.

**4:35 – Real documents (15 s, say it)**
> "On 22 real purchase-and-assumption agreements from 2009 to 2011, reading 3 percent of the pages, it got every buyer
> and date right and no wrong deal terms, for about a dollar."

**4:50 – Close (15 s)**
> "If I were deploying this: start every new bank at full review, measure silent errors instead of accuracy,
> reconcile against an independent source, and never overwrite a value. Happy to go into any part."

## 90-second verbal version (no screen)

> "I built a document AI pipeline for a scenario I find interesting: when a bank fails, the receiver has to turn the
> bank's loan files into trustworthy data within days. It uses Azure Document Intelligence to split and read the files,
> an LLM and simpler text models to flag problem loans from collector notes, a Power Apps review app for anything
> uncertain, Power Automate to apply decisions to a versioned record in Azure SQL, and Power BI to monitor quality
> and cost.
>
> I tested it on fictional banks the model had never seen. It reads 96 to almost 100 percent of values, and with
> confidence thresholds, validity rules and reconciliation against the bank's own schedule, zero wrong values slipped
> through, with about 2 percent sent to a person. The part I'd emphasize is that every safety check came from a new
> bank breaking something, which is why I'd start any new bank at full review. On 22 real public agreements from the
> 2009 to 2011 failures, it read the deal terms with no wrong values."

## Likely follow-ups (short answers)

| Question | Answer |
|---|---|
| Why synthetic data? | Real loan files are confidential borrower records. Synthetic files with planted answers let me measure exactly; the real agreements show it on real scans. |
| What broke? | Rules from familiar banks: 0% at a new bank. One augmentation recipe made the model worse (random omissions taught it "grab the nearest dollar amount"). Date order. Multi-page documents split in two. |
| How do you know an LLM answer is right? | Every value must carry a verbatim quote, checked against the page, plus a relevance check; a typed schema rejects malformed answers. A quote can be real and still the wrong clause: that's why relevance is checked too. |
| What would change in production? | Private networking, a service identity for the flows, row-level security in Power BI, and a pilot on real redacted files before any accuracy claim. |
| Why a loan view and not just the queue? | The unit of work changes. Reviewers fix values one at a time, but loans are sold in pools by type and performance, and the checks that matter compare documents with each other. Release is per loan, blocked until every value is settled. |
| How do you stop the agent making things up? | It can't write SQL or reach anything else: two read-only tools, fixed queries and a document search, and a database user with SELECT on exactly those tables. Every quote in an answer is checked word-for-word against what the tools returned, and a 15-question evaluation (answers computed from SQL, plus attacks: prompt leak, bid advice, an instruction to release a loan, a real-person lookup) passes 15/15. |
| Why SQL full-text instead of a vector search service? | About 950 passages: SQL full-text ranks them well, keeps the text next to the loan data, and adds no monthly cost. With thousands of real loan files I'd move to Azure AI Search (hybrid + semantic ranking), as in my other project. |
| How do you decide what to review first? | Expected value: chance the value is wrong (learned from other banks and this bank's decisions so far, never this bank's answer key) times the loan's balance. A trigger re-ranks the queue on every decision, so corrections feed back: active learning in its simplest form. |
| What did the red team find? | Before the guardrails, 2 of 18 attacks worked: a fake FDIC notice redirecting borrowers' payments, and a note planted in a comment log that made the agent say a loan was approved for release. After screening documents with Prompt Shields and adding rules, 0 of 18; the shield removed 0 of 36 real passages. |
| What did the data mining find? | On 454 real 2008-2012 failures, construction lending alone wasn't enough to predict a costly failure; construction plus bad loans or no capital left was (71% costly vs 34% base, lift 2.1, beating a shuffled-label baseline). Loan-mix clusters barely differ in cost. Associations among failed banks, not causes. |
| Cost? | About $80 of Document Intelligence for the whole project; real agreements cost about $1 for 22 because only the needed pages are read. |

## Reset after a demo

Decided items leave the queue. To run the demo again with a fresh queue (the audit trail is kept; the reset itself is
logged):
```bash
.venv/bin/python src/load_sql.py --reset-demo --no-upload
.venv/bin/python src/review_projection.py --reviewer <work account UPN>
.venv/bin/python src/loan_view.py --reviewer <work account UPN> --reset-releases
.venv/bin/python src/prescribe.py --reviewer <work account UPN>     # re-ranks the queue; reinstalls the trigger
.venv/bin/python src/powerbi.py publish
```
