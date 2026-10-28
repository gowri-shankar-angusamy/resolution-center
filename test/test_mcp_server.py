"""End-to-end test of mcp_server.py over stdio, plus service-level checks.
Run with `python test_mcp_server.py`. Needs the `mcp` package (2.x)."""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

import policy
import rulebook as rb
from approvals import AUTO_EXECUTED, ApprovalService
from rc_service import ResolutionService

ROOT = Path(__file__).parent
TMP = Path(tempfile.mkdtemp())
SVC = ResolutionService(approvals=ApprovalService(TMP / 'svc_audit.jsonl'))
CASES = [json.loads(l) for l in open(ROOT / "data/cases_dev.jsonl", encoding="utf-8")]
LABEL_KEYS = {"gold_outcome", "gold_rule", "scenario", "is_hard"}


def test_service_list_cases_filters_and_clamps():
    inr = SVC.list_cases("INR", limit=5)
    assert len(inr["cases"]) == 5 and all(c["case_type"] == "INR" for c in inr["cases"])
    assert len(SVC.list_cases(limit=10_000)["cases"]) == len(CASES)   # clamped to 100, all 90 fit
    assert len(SVC.list_cases(limit=0)["cases"]) == 1


def test_service_decisions_match_policy_on_all_dev_cases():
    for c in CASES:
        d = SVC.decide_case(c["case_id"])
        assert d["final_outcome"] == rb.label_case(c)[0]
        assert d["status"] in (policy.READY, policy.PENDING, policy.ESCALATED)
        assert d["evidence_complete"] and not LABEL_KEYS & set(d)


def test_service_unknown_case_escalates_not_crashes():
    d = SVC.decide_case("NOPE")
    assert d["rule_id"] == "INVALID_CASE" and d["requires_human"]
    assert "error" in SVC.get_case_context("NOPE")


async def _stdio_roundtrip():
    from mcp import Client, StdioServerParameters
    log = TMP / "stdio_audit.jsonl"
    params = StdioServerParameters(command=sys.executable, args=["mcp_server.py"], cwd=str(ROOT),
                                   env={"RC_AUDIT_LOG": str(log)})
    async with Client(params) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert names == {"list_cases", "gather_evidence", "decide_case", "get_case_context",
                         "submit_for_approval", "list_approval_queue"}, names
        assert not any(w in n for n in names for w in ("approve_", "execute", "override", "review"))

        cid = CASES[0]["case_id"]
        r = await c.call_tool("decide_case", {"case_id": cid})
        assert not r.is_error
        out = r.structured_content
        assert out["case_id"] == cid and out["final_outcome"] == rb.label_case(CASES[0])[0]
        assert out["requires_human"] or out["status"] == policy.READY

        ctx = (await c.call_tool("get_case_context", {"case_id": cid})).structured_content
        assert ctx["fired_rule"]["id"] == out["rule_id"] and len(ctx["similar_cases"]) <= 3

        ev = (await c.call_tool("gather_evidence", {"case_id": cid})).structured_content
        assert ev["complete"] and not LABEL_KEYS & set(ev["case"])

        bad = (await c.call_tool("decide_case", {"case_id": "NOPE"})).structured_content
        assert bad["rule_id"] == "INVALID_CASE"

        # queueing writes to the temp audit log, never approves, and refuses a duplicate
        sub = (await c.call_tool("submit_for_approval", {"case_id": cid})).structured_content
        assert sub["status"] in (policy.PENDING, policy.ESCALATED, AUTO_EXECUTED), sub
        dup = (await c.call_tool("submit_for_approval", {"case_id": cid})).structured_content
        assert "already has an active record" in dup["error"]
        q = (await c.call_tool("list_approval_queue", {})).structured_content
        assert q["count"] == (0 if sub["status"] == AUTO_EXECUTED else 1)
    assert ApprovalService(log).verify_log() == (True, None)


def test_mcp_stdio_end_to_end():
    asyncio.run(_stdio_roundtrip())


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"{len(fns)} mcp tests passed")
