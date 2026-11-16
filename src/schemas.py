"""Typed contracts at the project's trust boundaries (Pydantic).

1. LoanTape: one loan's extracted values, with the validity and plausibility rules ON the record. Hard type/range
   problems and cross-field problems are both reported per field by `loan_problems()`; the router sends any field
   with a problem to a person. (Previously these rules lived in route.rule_fail.)
2. PATerms: the LLM's answer for a purchase-and-assumption agreement. Every term is a Quoted value: value + the
   exact quote + page. Used as the Azure OpenAI structured-output schema, so the model must answer in this shape.
   A value without a quote is dropped (never auto-accepted).
3. TextFlagReply: the LLM's answer for risk flags in free text; flag names are restricted to the known set.

Treating model output as untrusted input: validate at the boundary, not deep in the scoring code.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, model_validator


# ---------------------------------------------------------------- 1. loan tape
class LoanTape(BaseModel):
    """Values read from one loan's documents. All optional: a field the model didn't read is simply absent.
    Hard rules are Field constraints; soft (cross-field) rules are collected in `problems` instead of raising,
    because a suspicious value should go to review, not be rejected."""
    model_config = ConfigDict(extra="ignore")

    note_date: date | None = None
    original_principal: float | None = Field(None, gt=0)
    note_rate_pct: float | None = Field(None, gt=0, lt=25)
    monthly_payment: float | None = Field(None, gt=0)
    maturity_date: date | None = None
    current_upb: float | None = Field(None, ge=0)
    next_due_date: date | None = None
    appraised_value: float | None = Field(None, gt=0)
    # reference values from an independent source (the data-room schedule), used only by plausibility rules
    reference_principal: float | None = Field(None, gt=0)

    _problems: dict[str, str] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def _cross_field(self) -> "LoanTape":
        p = self._problems
        if self.maturity_date and self.note_date and self.maturity_date <= self.note_date:
            p["maturity_date"] = "maturity is not after the note date"
        if self.original_principal and self.monthly_payment:
            ratio = self.monthly_payment / self.original_principal
            if not 0.001 <= ratio <= 0.06:      # a monthly payment is 0.1%-6% of the loan; catches the two swapped
                p["original_principal"] = p["monthly_payment"] = f"payment is {ratio:.1%} of principal"
        ref = self.reference_principal
        if ref and self.current_upb is not None and self.current_upb > ref * 1.001:
            p["current_upb"] = "balance above the original loan amount"
        if ref and self.appraised_value:
            ltv = ref / self.appraised_value
            if not 0.3 <= ltv <= 1.6:            # loan-to-value at origination 30%-160%
                p["appraised_value"] = f"loan-to-value {ltv:.0%} is implausible"
        return self

    @property
    def problems(self) -> dict[str, str]:
        return dict(self._problems)


def loan_problems(values: dict) -> dict[str, str]:
    """field → reason, for every value that breaks a hard or soft rule. Hard failures (wrong type, out of range)
    are reported for that field and the field is dropped, so cross-field rules still run on the rest."""
    vals = {k: v for k, v in values.items() if v not in (None, "")}
    problems: dict[str, str] = {}
    while True:
        try:
            tape = LoanTape.model_validate(vals)
            break
        except ValidationError as e:
            for err in e.errors():
                field = str(err["loc"][0]) if err["loc"] else "_record"
                problems[field] = err["msg"]
                vals.pop(field, None)
    problems.update(tape.problems)
    return problems


# ---------------------------------------------------------------- 2. P&A agreement terms (LLM structured output)
class QuotedText(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str | None = Field(description="the value, or null if the pages don't state it")
    quote: str | None = Field(description="exact text copied from the pages that states the value")
    page: int | None = Field(description="page number from the [page N] marker")


class QuotedNumber(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: float | None = Field(description="the number as stated (negative for a discount), or null; never computed")
    quote: str | None = Field(description="exact text copied from the pages that states the value")
    page: int | None = Field(description="page number from the [page N] marker")


class QuotedBool(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: bool | None = Field(description="true/false, or null if the pages don't say")
    quote: str | None = Field(description="exact text copied from the pages that supports the answer")
    page: int | None = Field(description="page number from the [page N] marker")


class PATerms(BaseModel):
    """Key terms of a failed-bank purchase-and-assumption agreement, each with its verbatim quote."""
    model_config = ConfigDict(extra="forbid")
    buyer: QuotedText = Field(description="the Assuming Institution's name")
    effective_date: QuotedText = Field(description="effective date of the agreement, YYYY-MM-DD")
    deposit_premium_pct: QuotedNumber = Field(description="deposit premium bid, percent")
    asset_premium_discount_usd: QuotedNumber = Field(description="asset premium (positive) or discount (negative) bid, dollars")
    loss_share: QuotedBool = Field(description="does the deal include shared-loss agreements")
    sf_loss_share_pct: QuotedNumber = Field(description="first Applicable Percentage of the single-family shared-loss agreement")
    commercial_loss_share_pct: QuotedNumber = Field(description="first Applicable Percentage of the commercial shared-loss agreement")

    @model_validator(mode="after")
    def _no_value_without_quote(self) -> "PATerms":
        for name in type(self).model_fields:
            q = getattr(self, name)
            if q.value is not None and not (q.quote and q.quote.strip()):
                q.value = None          # an unquoted value is never trusted
        # relevance: a loss-share percentage must be quoted from text about loss sharing (a verbatim quote can still be
        # the wrong clause, e.g. "fifty per cent (50%) of collections on Fully Charged-Off Assets")
        for name in ("sf_loss_share_pct", "commercial_loss_share_pct"):
            q = getattr(self, name)
            if q.value is not None and not re.search(r"loss", q.quote or "", re.I):
                q.value = None
        return self


# ---------------------------------------------------------------- 3. text risk flags (LLM)
FlagName = Literal["bankruptcy", "litigation", "hardship", "fraud_suspected", "property_damage", "payoff_request",
                   "underwriting_exception", "draw_without_inspection"]


class SegmentFlags(BaseModel):
    model_config = ConfigDict(extra="forbid")
    segment: int = Field(ge=1)
    flags: list[FlagName]


class TextFlagReply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    flags: list[SegmentFlags]
