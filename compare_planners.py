"""Compare deterministic and Ollama evidence planners on one dev case.

Example:
    python compare_planners.py RC-0231

This compares evidence gathering and the deterministic policy outcome only.
It does not submit, approve, or execute a case. Ollama mode requires the local
Ollama server and configured model to be running.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import policy
from evidence_agent import EvidenceAgent
from mock_apis import MockBackend
from rc_service import build_planner

ROOT = Path(__file__).parent


def load_cases() -> list[dict]:
    with open(ROOT / "data/cases_dev.jsonl", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def run_case(case_id: str, cases: list[dict], mode: str) -> dict:
    agent = EvidenceAgent(MockBackend(cases), planner=build_planner(mode))
    bundle = agent.gather(case_id)
    decision = policy.decide(bundle.to_case())
    planner_events = [event for event in bundle.trace if event.get("event") == "planner"]
    tool_events = [event for event in bundle.trace if event.get("event", "tool") == "tool"]
    return {
        "mode": mode,
        "case_id": case_id,
        "evidence_complete": bundle.complete,
        "failed_services": bundle.failed_services,
        "outcome": decision.final_outcome,
        "rule_id": decision.rule_id,
        "approval_status": decision.status,
        "planner_choices": [event.get("selected_service") for event in planner_events],
        "planner_calls": len(planner_events),
        "planner_latency_ms": round(sum(event.get("latency_ms", 0) for event in planner_events), 2),
        "tool_order": [event.get("service") for event in tool_events],
        "tool_latency_ms": round(sum(event.get("latency_ms", 0) for event in tool_events), 2),
        "tool_failures": [event for event in tool_events if not event.get("ok")],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_id", nargs="?", default="RC-0231", help="case ID from data/cases_dev.jsonl (default: RC-0231)")
    parser.add_argument("--modes", nargs="+", choices=("rule", "ollama"), default=["rule", "ollama"],
                        help="planner modes to run (default: rule ollama)")
    args = parser.parse_args()
    cases = load_cases()
    if not any(case.get("case_id") == args.case_id for case in cases):
        parser.error(f"{args.case_id!r} was not found in the dev case set")

    results = [run_case(args.case_id, cases, mode) for mode in args.modes]
    print(json.dumps({"case_id": args.case_id, "results": results,
                      "outcomes_match": len({(r["outcome"], r["rule_id"]) for r in results}) <= 1}, indent=2))
    return 0 if all(result["evidence_complete"] for result in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
