"""Compare AI-only, Policy-RAG, and explicit-policy advisor modes on one case.

Evidence is gathered once so the planner latency is measured once and does not
contaminate the comparison between advisor modes. This script is read-only: it
never submits, approves, or executes a case.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import policy
import rag_indexes as rag
from rc_service import ResolutionService, build_advisor

MODES = ("ai_only", "ai_policy_rag", "ai_explicit_policy")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_id", help="development case ID, e.g. RC-0085")
    args = parser.parse_args()

    service = ResolutionService()
    # This explicit comparison command opts into the advisor even if the live
    # console has the advisor disabled by default for the rule planner.
    service.advisor = build_advisor("ollama", planner_mode=service.planner_mode)
    service.advisor_mode = "ollama"
    original = service.get_case(args.case_id)
    if original is None:
        parser.error(f"unknown development case: {args.case_id}")

    bundle = service.agent.gather(args.case_id)
    case = bundle.to_case()
    decision = policy.decide(case)
    context = None if decision.rule_id == "INVALID_CASE" else rag.build_context(service.index, case, decision)
    planner_ms = round(sum(float(t.get("latency_ms") or 0) for t in bundle.trace if t.get("event") == "planner"), 2)
    tool_ms = round(sum(float(t.get("latency_ms") or 0) for t in bundle.trace if t.get("event") == "tool"), 2)

    results = []
    for mode in MODES:
        result = service.recommend_ai(case, context, advisor_mode=mode)
        results.append({
            "mode": mode,
            "status": result.status,
            "model": result.model,
            "latency_ms": result.latency_ms,
            "recommendation": result.to_dict().get("recommendation"),
            "error": result.error,
            "policy_context_sent": (result.details or {}).get("input_audit", {}).get("policy_context", {}),
        })

    print(json.dumps({
        "case_id": args.case_id,
        "evidence_complete": bundle.complete,
        "failed_services": bundle.failed_services,
        "planner_mode": service.planner_mode,
        "planner_latency_ms": planner_ms,
        "tool_latency_ms": tool_ms,
        "rulebook_outcome": decision.final_outcome,
        "rulebook_rule": decision.rule_id,
        "advisor_results": results,
        "note": "AI recommendations are advisory. The rulebook outcome and human-approval controls remain authoritative.",
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
