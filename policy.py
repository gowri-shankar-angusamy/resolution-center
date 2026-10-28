"""Deterministic policy layer: rules decide, AI advises, humans execute.

`decide` takes a case and an optional AI recommendation and returns a
`PolicyDecision`. The final outcome always comes from `rulebook.label_case`;
the AI recommendation is recorded and compared, never obeyed. The decision also
says whether a human must approve before anything is executed.

Approval policy (edit the constants here only):
  * REQUEST_EVIDENCE is the only outcome that can go out without approval, and
    only when nothing about the case looks risky.
  * APPROVE_BUYER moves money and DENY removes a buyer's remedy, so both need a
    human. ESCALATE routes to a senior reviewer.
  * Any AI/rule disagreement, low AI confidence or a risk flag forces review.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

import rulebook as rb

# Statuses
READY = "READY_TO_EXECUTE"            # safe to send automatically
PENDING = "PENDING_HUMAN_APPROVAL"    # a reviewer must approve the outcome
ESCALATED = "ESCALATED_TO_SENIOR"     # needs a senior reviewer, not a rubber stamp

AUTO_EXECUTE_OUTCOMES = {rb.REQUEST}
LOW_CONFIDENCE = 0.7                  # AI confidence below this forces review
HIGH_VALUE = rb.HIGH_VALUE

REQUIRED_FIELDS = (
    "case_id", "case_type", "order_amount", "order_date", "filed_date",
    "promised_delivery_date", "tracking_status", "delivery_proof",
    "address_match", "merchant_response", "prior_buyer_disputes_90d",
    "is_duplicate", "evidence_conflict", "return_status",
)
# The rulebook defaults these when absent; a missing value must escalate instead.
SNAD_FIELDS = (
    "snad_issue", "buyer_description_provided", "buyer_photo_provided",
    "listing_matches_item", "packaging_evidence",
)


@dataclass(frozen=True)
class AIRecommendation:
    outcome: str
    confidence: float
    rule_id: Optional[str] = None
    rationale: str = ""
    source: str = "kev"


@dataclass(frozen=True)
class PolicyDecision:
    case_id: str
    final_outcome: str                 # always the rulebook outcome
    rule_id: str
    status: str
    ai: Optional[AIRecommendation]
    ai_agrees: Optional[bool]          # None when there is no AI recommendation
    review_reasons: list[str] = field(default_factory=list)

    @property
    def requires_human(self) -> bool:
        return self.status != READY

    def to_dict(self) -> dict:
        d = asdict(self)
        d["requires_human"] = self.requires_human
        return d


def validate(case: dict) -> list[str]:
    """Return a list of problems; empty means the case is usable."""
    problems = [f"missing field: {f}" for f in REQUIRED_FIELDS if f not in case]
    if case.get("case_type") not in ("INR", "SNAD"):
        problems.append(f"unsupported case_type: {case.get('case_type')!r}")
    if case.get("case_type") == "SNAD":
        problems += [f"missing field: {f}" for f in SNAD_FIELDS if f not in case]
    amount = case.get("order_amount")
    if not isinstance(amount, (int, float)) or amount < 0:
        problems.append(f"invalid order_amount: {amount!r}")
    return problems


def decide(case: dict, ai: Optional[AIRecommendation] = None) -> PolicyDecision:
    problems = validate(case)
    if problems:
        # Malformed input is never auto-decided.
        return PolicyDecision(
            case_id=str(case.get("case_id", "?")), final_outcome=rb.ESCALATE,
            rule_id="INVALID_CASE", status=ESCALATED, ai=ai, ai_agrees=None,
            review_reasons=problems,
        )

    outcome, rule = rb.label_case(case)
    reasons: list[str] = []

    ai_agrees: Optional[bool] = None
    if ai is not None:
        if ai.outcome not in rb.OUTCOMES:
            reasons.append(f"AI returned unknown outcome {ai.outcome!r}")
            ai_agrees = False
        else:
            ai_agrees = ai.outcome == outcome
            if not ai_agrees:
                reasons.append(f"AI recommended {ai.outcome} but rulebook says {outcome} ({rule})")
        if ai.confidence < LOW_CONFIDENCE:
            reasons.append(f"AI confidence {ai.confidence:.2f} below {LOW_CONFIDENCE}")

    if case["order_amount"] >= HIGH_VALUE and outcome != rb.REQUEST:
        reasons.append(f"high-value case (amount >= {HIGH_VALUE})")
    if rule == rb.DEFAULT_RULE:
        reasons.append("no specific rule matched (default fall-through)")

    if outcome == rb.ESCALATE:
        status = ESCALATED
        reasons.append(f"rule {rule} requires escalation")
    elif outcome in AUTO_EXECUTE_OUTCOMES and not reasons:
        status = READY
    else:
        status = PENDING
        if outcome not in AUTO_EXECUTE_OUTCOMES:
            reasons.append(f"{outcome} is a consequential action")

    return PolicyDecision(case["case_id"], outcome, rule, status, ai, ai_agrees, reasons)
