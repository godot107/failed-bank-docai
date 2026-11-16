"""Free text for servicer comment logs, borrower letters and credit memos.

Each bank "style" writes differently (abbreviations, tone, phrasing), and test banks use styles that
never appear in training, so the text-flag evaluation measures generalization to a new bank's staff.
Every planted flag sentence is recorded verbatim, so a flag's quote can be checked exactly.
Decoy sentences mention a flag's topic without meaning it (e.g. "advised no bankruptcy filing on record").
"""
from __future__ import annotations

import random

# flag → style → phrasings ({b} = borrower, {amt} = amount, {d} = date)
FLAG_TEXT = {
    "bankruptcy": {
        "S1": ["Borrower called to advise Chapter 7 petition filed; case number to follow.",
               "Rec'd notice of Ch 13 filing from debtor's counsel. Placed acct on BK hold."],
        "S2": ["Bankruptcy notice received - borrower filed under Chapter 11. Collection activity suspended.",
               "Debtor filed bankruptcy petition; automatic stay in effect per attorney letter."],
        "S3": ["BK FILED CH7 - STOP ALL CONTACT PER POLICY.", "BK NOTICE RCVD, ACCT CODED BK13."],
        "S4": ["The customer informed us that they have entered bankruptcy proceedings.",
               "We were notified that the obligor sought protection under the bankruptcy code."],
        "S5": ["obligor in bk per court notice - refer to legal", "bk petition on file w/ district court, freeze collections"],
    },
    "litigation": {
        "S1": ["Borrower's attorney sent letter disputing the foreclosure; litigation pending.",
               "Suit filed by guarantor contesting the deficiency."],
        "S2": ["Lawsuit filed against the bank by the borrower alleging lender liability.",
               "Counsel advises a mechanic's lien foreclosure action has been filed on the collateral."],
        "S3": ["ATTY LTR RCVD - BORR CONTESTING FC.", "LITIGATION FLAG SET - SUIT FILED BY BORR."],
        "S4": ["The obligor has retained counsel and initiated a civil action regarding the loan.",
               "A complaint was served on the bank concerning this account."],
        "S5": ["borr sued over fc notice - hand to legal", "pending suit re: lien priority, hold workout"],
    },
    "hardship": {
        "S1": ["Borrower lost job in March and requests a forbearance plan.",
               "Borrower asked about loan modification due to reduced hours."],
        "S2": ["Customer reports medical hardship and asked to defer three payments.",
               "Borrower submitted a hardship letter requesting a modification of terms."],
        "S3": ["HARDSHIP - BORR UNEMPLOYED, REQ FORB.", "BORR REQ MOD, INCOME DOWN 40%."],
        "S4": ["The customer described a financial hardship and requested temporary payment relief.",
               "We received a request to restructure the payment schedule due to a business downturn."],
        "S5": ["borr can't make pmts since tenant left, asking for relief", "req for pmt deferral, cash flow problems"],
    },
    "fraud_suspected": {
        "S1": ["Appraisal review indicates value may have been inflated; referred to fraud unit.",
               "Employment could not be verified; possible misrepresentation on application."],
        "S2": ["Identity documents appear altered; suspicious activity report under consideration.",
               "Straw buyer suspected - title shows quick resale at a large markup."],
        "S3": ["POSS FRAUD - INCOME DOCS DON'T MATCH IRS TRANSCRIPT.", "REFER TO FRAUD - SIGNATURE MISMATCH."],
        "S4": ["Discrepancies in the borrower's documentation suggest possible misrepresentation.",
               "The collateral valuation appears to have been materially overstated at origination."],
        "S5": ["looks like fake w2s - escalate", "title flip 2 wks before closing, poss fraud"],
    },
    "property_damage": {
        "S1": ["Borrower reports roof damage from storm; insurance claim filed.",
               "Property inspection found the house vacant with broken windows."],
        "S2": ["Fire damage reported at collateral property; adjuster assigned.",
               "Inspector notes water damage and the building is unoccupied."],
        "S3": ["PROP DMG - HAIL, CLAIM #PENDING.", "INSP: VACANT, VANDALIZED."],
        "S4": ["A site visit confirmed significant physical damage to the collateral.",
               "The collateral was reported vacant and in deteriorating condition."],
        "S5": ["flood dmg at property per borr", "site vacant, copper stripped"],
    },
    "payoff_request": {
        "S1": ["Borrower requested a payoff quote; refinancing with another lender.",
               "Title company ordered a payoff statement for a pending sale."],
        "S2": ["Payoff letter requested by the borrower for a refinance.",
               "Borrower plans to sell the property and asked for the payoff amount."],
        "S3": ["PAYOFF REQ - REFI W/ CU.", "PAYOFF STMT SENT TO TITLE CO."],
        "S4": ["The customer asked for the amount needed to pay the loan in full.",
               "A settlement agent requested payoff figures for a scheduled closing."],
        "S5": ["payoff qt sent, borr selling", "refi payoff requested by new lender"],
    },
}

# Credit memo sentences (underwriting judgment)
MEMO_FLAG = {
    "underwriting_exception": {
        "S1": "Approved as an exception to policy: debt-to-income exceeds the 45% guideline.",
        "S2": "Loan committee approved despite the loan-to-value ratio exceeding policy limits.",
        "S3": "EXCEPTION APPROVED - STATED INCOME, NO TAX RETURNS OBTAINED.",
        "S4": "The committee granted a policy exception notwithstanding incomplete income documentation.",
        "S5": "ok'd as exception, ltv over limit, strong relationship",
    },
    "draw_without_inspection": {
        "S1": "Draw request #6 was funded before the site inspection was completed.",
        "S2": "Construction advance released without a current inspection report.",
        "S3": "DRAW FUNDED - INSP NOT RCVD.",
        "S4": "Funds were disbursed for the latest draw prior to inspector verification.",
        "S5": "paid draw 5, inspector hasn't been out yet",
    },
}

DECOY_TEXT = {
    "bankruptcy": {
        "S1": "Borrower asked whether bankruptcy would affect the loan; no filing on record.",
        "S2": "Checked PACER: no bankruptcy filing found for the borrower.",
        "S3": "BK SEARCH NEG.",
        "S4": "A search of court records showed no bankruptcy filing by the obligor.",
        "S5": "no bk on file per search",
    },
    "litigation": {
        "S1": "Borrower mentioned a lawsuit with a former employer, unrelated to this loan.",
        "S2": "No pending litigation involving the collateral per title update.",
        "S3": "LIT SEARCH NEG.",
        "S4": "Legal review found no claims or suits related to this account.",
        "S5": "no suits on title",
    },
    "fraud_suspected": {
        "S1": "Annual fraud-awareness review completed for the file; no issues found.",
        "S2": "Identity verified against two forms of ID; no fraud indicators.",
        "S3": "FRAUD SCRN CLEAR.",
        "S4": "Standard fraud screening returned no exceptions.",
        "S5": "fraud check clean",
    },
}

ROUTINE = {
    "S1": ["Payment received, posted to account.", "Escrow analysis completed; no shortage.",
           "Called borrower, left voicemail re: upcoming payment.", "Annual statement mailed."],
    "S2": ["Payment processed via ACH.", "Customer updated mailing address.", "Escrow disbursement for county taxes.",
           "Courtesy reminder sent."],
    "S3": ["PMT RCVD.", "LVM RE PMT.", "ESC ANALYSIS DONE.", "ADDR UPDATED."],
    "S4": ["The scheduled payment was received and applied.", "The customer confirmed receipt of the statement.",
           "Routine account review completed."],
    "S5": ["pmt in", "lvm", "stmt mailed", "called, no answer"],
}


def comment_log(rng: random.Random, style: str, flags: list[str], decoys: list[str], dates: list[str]):
    """Entries (date, text, flag, decoy) for the servicer comment log. Flags are spread among routine notes."""
    entries = []
    for f in flags:
        if f in FLAG_TEXT:
            entries.append({"text": rng.choice(FLAG_TEXT[f][style]), "flag": f, "decoy": False})
    for f in decoys:
        entries.append({"text": DECOY_TEXT[f][style], "flag": None, "decoy": f})
    for _ in range(rng.randint(4, 8)):
        entries.append({"text": rng.choice(ROUTINE[style]), "flag": None, "decoy": False})
    rng.shuffle(entries)
    ds = sorted(rng.sample(dates, k=min(len(dates), len(entries))))
    while len(ds) < len(entries):
        ds.append(ds[-1])
    for e, d in zip(entries, ds):
        e["date"] = d
    return entries


def memo_sentences(rng: random.Random, style: str, flags: list[str]):
    return [{"text": MEMO_FLAG[f][style], "flag": f} for f in flags if f in MEMO_FLAG]


LETTER_OPEN = {
    "S1": "To whom it may concern,", "S2": "Dear Loan Servicing Department,", "S3": "RE: LOAN NUMBER {ln}",
    "S4": "Dear Sir or Madam,", "S5": "Hello,",
}


def borrower_letter(rng: random.Random, style: str, flags: list[str], loan_number: str):
    """A short borrower letter when the story has a borrower-voiced flag (hardship, payoff, dispute)."""
    voiced = [f for f in flags if f in ("hardship", "payoff_request", "litigation")]
    if not voiced:
        return None
    f = rng.choice(voiced)
    body = {
        "hardship": "I am writing because I have fallen behind on my payments. My income dropped this year and I am "
                    "asking for a modification or a temporary reduction of my payment until I am back on my feet.",
        "payoff_request": "Please send me the payoff amount for my loan. I am refinancing with another lender and the "
                          "closing is scheduled for next month.",
        "litigation": "My attorney will be contacting you. I dispute the foreclosure notice I received and I have "
                      "filed a complaint with the court regarding this matter.",
    }[f]
    return {"opening": LETTER_OPEN[style].format(ln=loan_number), "text": body, "flag": f}
