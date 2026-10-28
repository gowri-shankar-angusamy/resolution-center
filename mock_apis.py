"""Mock backend services for the Resolution Center.

Each service answers from a synthetic case record, but only exposes the slice
of fields a real system of that kind would own. Gold labels, scenario names and
`is_hard` are never part of a case file's service views, so nothing here can leak
them to an agent or model.

Failure injection: `MockBackend(failures={"tracking": {"RC-0001": 1}})` makes the
first N calls for that case raise a transient `ToolError`; use `PERMANENT` for a
service that never answers.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Optional

PERMANENT = 10**9


class ToolError(Exception):
    def __init__(self, message: str, transient: bool = True):
        super().__init__(message)
        self.transient = transient


# service -> fields it owns
SERVICE_FIELDS: dict[str, tuple[str, ...]] = {
    "order": ("item", "order_amount", "order_date", "promised_delivery_date"),
    "tracking": ("tracking_status", "delivery_proof", "address_match"),
    "dispute": ("case_type", "filed_date", "is_duplicate", "evidence_conflict",
                "return_status", "buyer_statement", "buyer_evidence"),
    "merchant": ("merchant_response", "merchant_statement", "merchant_evidence"),
    "buyer_history": ("prior_buyer_disputes_90d",),
    "snad_claim": ("snad_issue", "buyer_description_provided", "buyer_photo_provided",
                   "listing_matches_item", "packaging_evidence"),
}


class MockBackend:
    def __init__(self, cases: list[dict], failures: Optional[dict[str, dict[str, int]]] = None):
        self._cases = {c["case_id"]: c for c in cases}
        self._remaining = defaultdict(int)
        for service, per_case in (failures or {}).items():
            for case_id, n in per_case.items():
                self._remaining[(service, case_id)] = n
        self.call_log: list[tuple[str, str]] = []

    def call(self, service: str, case_id: str) -> dict:
        self.call_log.append((service, case_id))
        if service not in SERVICE_FIELDS:
            raise ToolError(f"unknown service {service!r}", transient=False)
        case = self._cases.get(case_id)
        if case is None:
            raise ToolError(f"case {case_id} not found", transient=False)
        if self._remaining[(service, case_id)] > 0:
            self._remaining[(service, case_id)] -= 1
            raise ToolError(f"{service} service unavailable", transient=True)
        # snad_claim only exists for SNAD cases
        if service == "snad_claim" and case["case_type"] != "SNAD":
            raise ToolError("no SNAD claim on this case", transient=False)
        return {f: case[f] for f in SERVICE_FIELDS[service] if f in case}
