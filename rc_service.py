"""Resolution Center service: the logic behind the MCP tools, with no MCP import.

Wires together mock_apis -> evidence_agent -> policy -> rag_indexes. Dev split
only. Nothing here executes a refund or denial; `decide_case` returns a
recommendation with the approval status a human reviewer must act on.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import policy
import rag_indexes as rag
from approvals import ApprovalError, ApprovalService
from evidence_agent import EvidenceAgent
from mock_apis import MockBackend

ROOT = Path(__file__).parent


class ResolutionService:
    def __init__(self, cases_path: Path = ROOT / "data/cases_dev.jsonl",
                 approvals: Optional[ApprovalService] = None):
        with open(cases_path, encoding="utf-8") as f:
            self._cases = [json.loads(line) for line in f]
        self.agent = EvidenceAgent(MockBackend(self._cases))
        self.index = rag.build_multi_index()
        self.approvals = approvals or ApprovalService()

    def list_cases(self, case_type: Optional[str] = None, limit: int = 20) -> dict:
        rows = [{"case_id": c["case_id"], "case_type": c["case_type"], "order_amount": c["order_amount"]}
                for c in self._cases if case_type in (None, c["case_type"])]
        limit = max(1, min(limit, 100))
        return {"total": len(rows), "cases": rows[:limit]}

    def gather_evidence(self, case_id: str) -> dict:
        b = self.agent.gather(case_id)
        return {"case_id": case_id, "complete": b.complete,
                "failed_services": b.failed_services, "trace": b.trace, "case": b.to_case()}

    def get_case(self, case_id: str) -> Optional[dict]:
        return next((c for c in self._cases if c["case_id"] == case_id), None)

    def decide_case(self, case_id: str, ai: Optional[policy.AIRecommendation] = None) -> dict:
        b = self.agent.gather(case_id)
        d = policy.decide(b.to_case(), ai)
        return {**d.to_dict(), "evidence_complete": b.complete,
                "note": "Recommendation only. Nothing has been executed."}

    def submit_for_approval(self, case_id: str, ai: Optional[policy.AIRecommendation] = None) -> dict:
        """Queue a recommendation for human review. Never approves or executes it."""
        b = self.agent.gather(case_id)
        case = b.to_case()
        try:
            return self.approvals.submit(case, policy.decide(case, ai))
        except ApprovalError as e:
            return {"case_id": case_id, "error": str(e)}

    def list_approval_queue(self, status: Optional[str] = None) -> dict:
        items = self.approvals.queue(status)
        return {"count": len(items), "items": items}

    def get_case_context(self, case_id: str) -> dict:
        b = self.agent.gather(case_id)
        case = b.to_case()
        d = policy.decide(case)
        if d.rule_id == "INVALID_CASE":
            return {"case_id": case_id, "error": "case incomplete or unknown",
                    "problems": d.review_reasons}
        return rag.build_context(self.index, case, d)
