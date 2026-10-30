"""Resolution Center service: the logic behind the MCP tools, with no MCP import.

Wires together mock_apis -> evidence_agent -> policy -> rag_indexes. Dev split
only. Nothing here executes a refund or denial; `decide_case` returns a
recommendation with the approval status a human reviewer must act on.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import policy
import rag_indexes as rag
from approvals import ApprovalError, ApprovalService
from ai_advisor import AdvisorResult, OllamaAdvisor
from evidence_agent import EvidenceAgent, RulePlanner
from mock_apis import MockBackend
from mcp_evidence_client import MCPBackend
from ollama_planner import OllamaPlanner

ROOT = Path(__file__).parent


def build_planner(mode: Optional[str] = None, tool_catalog=None):
    """Build the configured evidence planner.

    RC_PLANNER defaults to ``rule`` so existing workflows remain deterministic.
    Set RC_PLANNER=ollama to opt into local Qwen/Ollama planning.
    """
    selected = (mode or os.getenv("RC_PLANNER", "rule")).strip().lower()
    if selected == "rule":
        return RulePlanner()
    if selected == "ollama":
        try:
            timeout = int(os.getenv("OLLAMA_TIMEOUT", "45"))
        except ValueError as exc:
            raise ValueError("OLLAMA_TIMEOUT must be an integer number of seconds") from exc
        if timeout <= 0:
            raise ValueError("OLLAMA_TIMEOUT must be greater than zero")
        return OllamaPlanner(
            model=os.getenv("OLLAMA_MODEL", "qwen3.5:4b"),
            host=os.getenv("OLLAMA_HOST", "http://localhost:11434"),
            timeout=timeout,
            think=False,
            tool_catalog=tool_catalog,
        )
    raise ValueError(
        f"Unsupported RC_PLANNER={selected!r}; choose 'rule' or 'ollama'."
    )


def build_advisor(mode: Optional[str] = None, planner_mode: Optional[str] = None):
    """Build the optional AI advisor. It defaults on with Ollama planning and off otherwise."""
    selected_planner = (planner_mode or os.getenv("RC_PLANNER", "rule")).strip().lower()
    default_mode = "ollama" if selected_planner == "ollama" else "none"
    selected = (mode or os.getenv("RC_AI_ADVISOR", default_mode)).strip().lower()
    if selected in ("none", "off", "disabled"):
        return None
    if selected == "ollama":
        try:
            timeout = int(os.getenv("OLLAMA_ADVISOR_TIMEOUT", os.getenv("OLLAMA_TIMEOUT", "60")))
        except ValueError as exc:
            raise ValueError("OLLAMA_ADVISOR_TIMEOUT must be an integer number of seconds") from exc
        if timeout <= 0:
            raise ValueError("OLLAMA_ADVISOR_TIMEOUT must be greater than zero")
        return OllamaAdvisor(
            model=os.getenv("OLLAMA_ADVISOR_MODEL", os.getenv("OLLAMA_MODEL", "qwen3.5:4b")),
            host=os.getenv("OLLAMA_HOST", "http://localhost:11434"),
            timeout=timeout,
        )
    raise ValueError(f"Unsupported RC_AI_ADVISOR={selected!r}; choose 'ollama' or 'none'.")


class ResolutionService:
    def __init__(self, cases_path: Path = ROOT / "data/cases_dev.jsonl",
                 approvals: Optional[ApprovalService] = None,
                 planner_mode: Optional[str] = None):
        with open(cases_path, encoding="utf-8") as f:
            self._cases = [json.loads(line) for line in f]
        self.planner_mode = (planner_mode or os.getenv("RC_PLANNER", "rule")).strip().lower()
        transport = os.getenv("RC_EVIDENCE_TRANSPORT", "direct").strip().lower()
        if transport == "direct":
            backend = MockBackend(self._cases)
        elif transport == "mcp":
            # MCPBackend starts one persistent, separate evidence server process.
            # That server exposes granular tools and calls MockBackend directly;
            # it never calls ResolutionService or EvidenceAgent, avoiding recursion.
            backend = MCPBackend(cases_path=cases_path)
        else:
            raise ValueError("Unsupported RC_EVIDENCE_TRANSPORT; choose 'direct' or 'mcp'.")
        self.evidence_transport = transport
        self.agent = EvidenceAgent(backend, planner=build_planner(
            self.planner_mode, tool_catalog=backend.list_tools if transport == "mcp" else None))
        self.advisor = build_advisor(planner_mode=self.planner_mode)
        self.advisor_mode = "ollama" if self.advisor is not None else "none"
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

    def recommend_ai(self, case: dict, context: Optional[dict] = None,
                     advisor_mode: str = "ai_only") -> AdvisorResult:
        """Run the optional advisor; failures are reported and never alter rulebook authority."""
        if self.advisor is None:
            return AdvisorResult("disabled", None, 0.0, "",
                                 error="AI advisor disabled; set RC_AI_ADVISOR=ollama to enable it.")
        return self.advisor.recommend(case, context, mode=advisor_mode)

    def decide_case(self, case_id: str, ai: Optional[policy.AIRecommendation] = None,
                    advisor_mode: str = "ai_only") -> dict:
        b = self.agent.gather(case_id)
        case = b.to_case()
        base = policy.decide(case)
        context = None if base.rule_id == "INVALID_CASE" else rag.build_context(self.index, case, base)
        advisor_result = self.recommend_ai(case, context, advisor_mode) if ai is None else None
        recommendation = ai or (advisor_result.recommendation if advisor_result else None)
        d = policy.decide(case, recommendation)
        return {**d.to_dict(), "evidence_complete": b.complete,
                "advisor": advisor_result.to_dict() if advisor_result else None,
                "note": "AI is advisory only. The deterministic rulebook remains authoritative; nothing has been executed."}

    def submit_for_approval(self, case_id: str, ai: Optional[policy.AIRecommendation] = None,
                            advisor_mode: str = "ai_only") -> dict:
        """Queue a recommendation for human review. Never approves or executes it."""
        b = self.agent.gather(case_id)
        case = b.to_case()
        try:
            base = policy.decide(case)
            context = None if base.rule_id == "INVALID_CASE" else rag.build_context(self.index, case, base)
            advisor_result = self.recommend_ai(case, context, advisor_mode) if ai is None else None
            recommendation = ai or (advisor_result.recommendation if advisor_result else None)
            queued = self.approvals.submit(case, policy.decide(case, recommendation))
            if advisor_result is not None:
                queued["advisor"] = advisor_result.to_dict()
            return queued
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
