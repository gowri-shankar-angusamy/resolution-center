"""Tests for mock_apis.py and evidence_agent.py."""
import json

import policy
import rulebook as rb
from evidence_agent import MAX_CALLS, EvidenceAgent
from mock_apis import PERMANENT, MockBackend, ToolError


def load():
    with open("data/cases_dev.jsonl") as f:
        return [json.loads(l) for l in f]


CASES = load()
INR = next(c for c in CASES if c["case_type"] == "INR")
SNAD = next(c for c in CASES if c["case_type"] == "SNAD")
FREE_TEXT = {"buyer_statement", "merchant_statement", "buyer_evidence", "merchant_evidence", "item"}


def test_gathered_case_matches_original_on_every_dev_case():
    agent = EvidenceAgent(MockBackend(CASES))
    for c in CASES:
        b = agent.gather(c["case_id"])
        assert b.complete
        assert b.to_case() == c                       # nothing lost or added
        assert rb.label_case(b.to_case()) == rb.label_case(c)


def test_inr_skips_snad_claim_and_snad_includes_it():
    backend = MockBackend(CASES)
    agent = EvidenceAgent(backend)
    assert [t["service"] for t in agent.gather(INR["case_id"]).trace][-1] == "buyer_history"
    assert [t["service"] for t in agent.gather(SNAD["case_id"]).trace][-1] == "snad_claim"


def test_transient_failure_is_retried():
    backend = MockBackend(CASES, failures={"tracking": {INR["case_id"]: 1}})
    b = EvidenceAgent(backend).gather(INR["case_id"])
    assert b.complete
    tracking = next(t for t in b.trace if t["service"] == "tracking")
    assert tracking["attempts"] == 2 and tracking["ok"]


def test_permanent_failure_escalates_via_policy():
    backend = MockBackend(CASES, failures={"buyer_history": {INR["case_id"]: PERMANENT}})
    b = EvidenceAgent(backend).gather(INR["case_id"])
    assert not b.complete and "buyer_history" in b.failed_services
    d = policy.decide(b.to_case())
    assert d.rule_id == "INVALID_CASE" and d.status == policy.ESCALATED
    assert any("prior_buyer_disputes_90d" in r for r in d.review_reasons)


def test_missing_snad_claim_escalates():
    backend = MockBackend(CASES, failures={"snad_claim": {SNAD["case_id"]: PERMANENT}})
    d = policy.decide(EvidenceAgent(backend).gather(SNAD["case_id"]).to_case())
    assert d.status == policy.ESCALATED


def test_unknown_case_is_not_found():
    b = EvidenceAgent(MockBackend(CASES)).gather("NOPE")
    assert not b.complete and b.fields == {}


def test_bad_planner_cannot_loop_or_overspend():
    class Looper:
        def plan_next(self, state):
            return "order"          # keeps asking for the same tool

    backend = MockBackend(CASES)
    b = EvidenceAgent(backend, Looper()).gather(INR["case_id"])
    assert len(backend.call_log) <= MAX_CALLS
    assert not b.complete

    class Inventor:
        def plan_next(self, state):
            return "delete_everything"

    b = EvidenceAgent(MockBackend(CASES), Inventor()).gather(INR["case_id"])
    assert not b.complete


def test_trace_includes_planner_and_tool_timing_without_evidence_values():
    bundle = EvidenceAgent(MockBackend(CASES)).gather(INR["case_id"])
    planner_events = [event for event in bundle.trace if event.get("event") == "planner"]
    tool_events = [event for event in bundle.trace if event.get("event", "tool") == "tool"]
    assert planner_events and tool_events
    assert all(event["planner"] == "RulePlanner" for event in planner_events)
    assert all(isinstance(event["latency_ms"], (int, float)) for event in planner_events + tool_events)
    assert all("result_fields" in event and "fields" not in event for event in tool_events)
    assert all("available_services" in event for event in planner_events)


def test_services_do_not_expose_labels():
    backend = MockBackend(CASES)
    for service in ("order", "tracking", "dispute", "merchant", "buyer_history", "snad_claim"):
        try:
            out = backend.call(service, SNAD["case_id"])
        except ToolError:
            continue
        assert not {"gold_outcome", "gold_rule", "scenario", "is_hard"} & set(out)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"{len(fns)} agent tests passed")
