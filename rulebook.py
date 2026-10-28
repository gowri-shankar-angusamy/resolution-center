"""Dispute Rulebook v0.1 as code.

This is the single source of truth for gold labels. Every synthetic case is
labeled by `label_case`, never by a model. Each result carries the rule ID that
fired so model outputs can also be scored on rule citation.

Interpretations made while turning the prose rulebook into code (confirm these):
  * G4 vs G6: any evidence conflict escalates. G4 fires when amount >= 1,000,
    otherwise G6 fires. Order is G1, G2, G3, G4, G5, G6.
  * SNAD-6 is a gate on approvals: a SNAD case that would be APPROVE_BUYER with
    amount >= 100 and return_status == "not_shipped" becomes REQUEST_EVIDENCE.
    return_status "received" (or "not_required", "in_transit") leaves it APPROVE_BUYER.
  * Unknown address match with delivery proof matches neither INR-2 nor INR-3,
    so it falls through to the default REQUEST_EVIDENCE.
"""
from __future__ import annotations

from datetime import date
from typing import Any

APPROVE = "APPROVE_BUYER"
DENY = "DENY"
REQUEST = "REQUEST_EVIDENCE"
ESCALATE = "ESCALATE"
OUTCOMES = [APPROVE, DENY, REQUEST, ESCALATE]

# Placeholder thresholds from the rulebook. Edit here only.
FILING_WINDOW_DAYS = 180
HIGH_VALUE = 1000
ABUSE_PRIOR_DISPUTES = 3
NO_TRACKING_DAYS = 7
LONG_TRANSIT_DAYS = 14
RETURN_REQUIRED_AMOUNT = 100

DEFAULT_RULE = "DEFAULT"


def _d(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def label_case(case: dict) -> tuple[str, str]:
    """Return (outcome, rule_id) for a case dict following Rulebook v0.1."""
    order_date = _d(case["order_date"])
    filed = _d(case["filed_date"])
    promised = _d(case["promised_delivery_date"])
    amount = case["order_amount"]
    tracking = case["tracking_status"]
    proof = case["delivery_proof"]
    conflict = case.get("evidence_conflict", False)

    # ---- Global rules, in order ----
    if (filed - order_date).days > FILING_WINDOW_DAYS:
        return DENY, "G1"
    if case.get("is_duplicate", False):
        return DENY, "G2"
    if case["case_type"] == "SNAD" and case.get("snad_issue") == "counterfeit":
        return ESCALATE, "G3"
    if conflict and amount >= HIGH_VALUE:
        return ESCALATE, "G4"
    if case.get("prior_buyer_disputes_90d", 0) >= ABUSE_PRIOR_DISPUTES and proof in ("signature", "photo"):
        return ESCALATE, "G5"
    if conflict:
        return ESCALATE, "G6"

    if case["case_type"] == "INR":
        return _label_inr(case, filed, promised, tracking, proof)
    if case["case_type"] == "SNAD":
        return _label_snad(case, amount)
    return REQUEST, DEFAULT_RULE


def _label_inr(case, filed, promised, tracking, proof):
    days_past = (filed - promised).days
    if days_past < 0 and tracking not in ("lost", "returned_to_sender"):
        return DENY, "INR-1"
    if tracking == "delivered" and proof in ("signature", "photo") and case["address_match"] is True:
        return DENY, "INR-2"
    if tracking == "delivered" and (proof == "none" or case["address_match"] is False):
        return APPROVE, "INR-3"
    if tracking in ("lost", "returned_to_sender"):
        return APPROVE, "INR-4"
    if tracking == "none" and days_past >= NO_TRACKING_DAYS and case["merchant_response"] == "none":
        return APPROVE, "INR-5"
    if tracking == "none" and 0 <= days_past < NO_TRACKING_DAYS:
        return REQUEST, "INR-6"
    if tracking == "in_transit":
        if days_past >= LONG_TRANSIT_DAYS:
            return APPROVE, "INR-7"
        return DENY, "INR-7"
    return REQUEST, DEFAULT_RULE


def _label_snad(case, amount):
    if not case.get("buyer_description_provided") or not case.get("buyer_photo_provided"):
        return REQUEST, "SNAD-1"

    issue = case.get("snad_issue", "none")
    outcome, rule = REQUEST, DEFAULT_RULE
    if issue == "wrong_item":
        outcome, rule = APPROVE, "SNAD-2"
    elif issue == "attribute_mismatch" and case["merchant_response"] == "none":
        outcome, rule = APPROVE, "SNAD-3"
    elif issue == "subjective_only" and case.get("listing_matches_item", False):
        return DENY, "SNAD-4"
    elif issue == "damaged":
        if case.get("packaging_evidence", "none") == "sound":
            return ESCALATE, "SNAD-5"
        outcome, rule = APPROVE, "SNAD-5"

    # SNAD-6: return gate on approvals
    if outcome == APPROVE and amount >= RETURN_REQUIRED_AMOUNT and case.get("return_status") == "not_shipped":
        return REQUEST, "SNAD-6"
    return outcome, rule
