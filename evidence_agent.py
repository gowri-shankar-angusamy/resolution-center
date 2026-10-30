"""Evidence-gathering agent.

The adjudication itself is a fixed workflow (see policy.py). This agent only
decides which backend tools to call and in what order, then hands back an
`EvidenceBundle` that can be turned into a case dict for `policy.decide`.

The planner is pluggable: `plan_next(state) -> service name | None`. The default
`RulePlanner` is deterministic; an LLM planner can be dropped in later with the
same interface. The agent, not the planner, owns retries, the call budget and
the trace, so a planner cannot loop forever or skip error handling.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter
from typing import Optional, Protocol

from mock_apis import SERVICE_FIELDS, MockBackend, ToolError

MAX_ATTEMPTS = 2        # one retry for transient failures
MAX_CALLS = 12          # hard budget per case


@dataclass
class AgentState:
    case_id: str
    collected: dict = field(default_factory=dict)       # field -> value
    done: set = field(default_factory=set)               # services answered
    failed: dict = field(default_factory=dict)           # service -> error message

    @property
    def tried(self) -> set:
        return self.done | set(self.failed)


class Planner(Protocol):
    def plan_next(self, state: AgentState) -> Optional[str]: ...


class RulePlanner:
    """Order -> dispute -> tracking -> merchant -> history, plus the SNAD claim
    once the dispute record shows the case type."""

    BASE = ["order", "dispute", "tracking", "merchant", "buyer_history"]

    def plan_next(self, state: AgentState) -> Optional[str]:
        for service in self.BASE:
            if service not in state.tried:
                return service
        if state.collected.get("case_type") == "SNAD" and "snad_claim" not in state.tried:
            return "snad_claim"
        return None


@dataclass
class EvidenceBundle:
    case_id: str
    fields: dict
    trace: list[dict]
    failed_services: dict

    @property
    def complete(self) -> bool:
        return not self.failed_services

    def to_case(self) -> dict:
        """Case dict for policy.decide. Fields from failed services are absent,
        which policy.validate turns into an escalation."""
        return {"case_id": self.case_id, **self.fields}


class EvidenceAgent:
    def __init__(self, backend, planner: Optional[Planner] = None):
        # Backend implements call(service, case_id). It may be the direct mock
        # backend or an MCP-backed adapter; orchestration remains here so the
        # MCP server never calls back into this agent (no recursive MCP path).
        self.backend = backend
        self.planner = planner or RulePlanner()

    def gather(self, case_id: str) -> EvidenceBundle:
        state = AgentState(case_id)
        trace: list[dict] = []
        calls = 0
        while calls < MAX_CALLS:
            planner_started = perf_counter()
            service = self.planner.plan_next(state)
            planner_ms = round((perf_counter() - planner_started) * 1000, 2)
            planner_error = state.failed.get("__planner__")
            if service is not None or planner_error:
                trace.append({
                    "event": "planner",
                    "service": None,  # preserves the existing trace schema for callers
                    "planner": type(self.planner).__name__,
                    "selected_service": service or "(stop)",
                    "ok": not bool(planner_error),
                    "attempts": 1,
                    "latency_ms": planner_ms,
                    "available_services": sorted(
                        name for name in SERVICE_FIELDS
                        if name not in state.tried
                        and (name != "snad_claim" or state.collected.get("case_type") == "SNAD")
                    ),
                    "error": planner_error,
                })
            if service is None:
                break
            if service not in SERVICE_FIELDS or service in state.tried:
                # Planner asked for something invalid or repeated: record and stop
                # rather than let it burn the budget.
                error = "invalid or repeated tool request"
                trace.append({"event": "tool", "service": service, "ok": False,
                              "attempts": 0, "latency_ms": 0.0, "result_fields": [],
                              "error": error})
                state.failed[str(service)] = error
                break
            attempts, error, result = 0, None, None
            tool_started = perf_counter()
            while attempts < MAX_ATTEMPTS and calls < MAX_CALLS:
                attempts += 1
                calls += 1
                try:
                    result = self.backend.call(service, case_id)
                    error = None
                    break
                except ToolError as e:
                    error = str(e)
                    if not e.transient:
                        break
            tool_ms = round((perf_counter() - tool_started) * 1000, 2)
            if result is not None:
                state.collected.update(result)
                state.done.add(service)
            else:
                state.failed[service] = error or "call budget exhausted"
            trace.append({"event": "tool", "service": service, "ok": result is not None,
                          "attempts": attempts, "latency_ms": tool_ms,
                          "result_fields": sorted(result.keys()) if result is not None else [],
                          "error": error})
        return EvidenceBundle(case_id, state.collected, trace, state.failed)
