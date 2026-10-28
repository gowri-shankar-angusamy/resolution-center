"""Tests for approvals.py. Run with `python test_approvals.py` or `pytest`."""
import json
import tempfile
from pathlib import Path

import policy
import rulebook as rb
from approvals import (APPROVED, ESCALATED, EXECUTED, OVERRIDDEN, PENDING, REJECTED, ApprovalError,
                       ApprovalService, AUTO_EXECUTED)
from policy import AIRecommendation
from test_rulebook import mk


def svc():
    return ApprovalService(Path(tempfile.mkdtemp()) / "audit.jsonl", clock=lambda: "2026-01-01T00:00:00+00:00")


def case(case_id="C-1", case_type="INR", **kw):
    c = mk(case_type, **kw)
    c["case_id"] = case_id
    return c


def submit(s, c, ai=None):
    return s.submit(c, policy.decide(c, ai))


def raises(fn, text):
    try:
        fn()
    except ApprovalError as e:
        assert text in str(e), str(e)
        return
    raise AssertionError(f"expected ApprovalError containing {text!r}")


LOST = dict(tracking_status="lost")                  # INR-4 -> APPROVE_BUYER, PENDING
ESC = dict(case_type="SNAD", snad_issue="counterfeit")  # G3 -> ESCALATE


def test_approve_then_execute_happy_path():
    s = svc()
    assert submit(s, case(**LOST))["status"] == PENDING
    assert s.review("C-1", "alice", "reviewer", "approve")["status"] == APPROVED
    st = s.execute("C-1", "alice", "reviewer")
    assert st["status"] == EXECUTED and st["final_outcome"] == rb.APPROVE
    assert [h["event"] for h in st["history"]] == ["RECOMMENDED", "REVIEWED", "EXECUTED"]


def test_cannot_execute_without_approval_or_twice():
    s = svc()
    submit(s, case(**LOST))
    raises(lambda: s.execute("C-1", "alice", "reviewer"), "not approved")
    s.review("C-1", "alice", "reviewer", "approve")
    s.execute("C-1", "alice", "reviewer")
    raises(lambda: s.execute("C-1", "alice", "reviewer"), "not approved")
    raises(lambda: s.review("C-1", "bob", "reviewer", "reject", "late"), "not open")


def test_clean_request_evidence_is_auto_executed_by_system():
    s = svc()
    st = submit(s, case(tracking_status="none", days_past=3), AIRecommendation(rb.REQUEST, 0.95))
    assert st["status"] == AUTO_EXECUTED and not s.queue()
    assert [e["actor"] for e in s.events()] == ["system", "system"]


def test_escalated_and_high_value_need_senior():
    s = svc()
    submit(s, case("E-1", **ESC))
    assert s.queue()[0]["status"] == ESCALATED
    raises(lambda: s.review("E-1", "alice", "reviewer", "approve"), "senior")
    assert s.review("E-1", "sam", "senior", "approve")["status"] == APPROVED

    submit(s, case("H-1", tracking_status="lost", order_amount=2500))
    raises(lambda: s.review("H-1", "alice", "reviewer", "approve"), "senior")
    # a reviewer may still reject a high-value case
    assert s.review("H-1", "alice", "reviewer", "reject", "needs more info")["status"] == REJECTED


def test_override_rules():
    s = svc()
    submit(s, case(**LOST))                                          # recommends APPROVE_BUYER
    raises(lambda: s.review("C-1", "alice", "reviewer", "override", override_outcome=rb.DENY), "reason")
    raises(lambda: s.review("C-1", "alice", "reviewer", "override", "x", rb.APPROVE), "change the outcome")
    raises(lambda: s.review("C-1", "alice", "reviewer", "override", "x", "BOGUS"), "override_outcome")
    raises(lambda: s.review("C-1", "alice", "reviewer", "approve", "", rb.DENY), "only valid")
    st = s.review("C-1", "alice", "reviewer", "override", "buyer abuse pattern", rb.DENY)
    assert st["status"] == OVERRIDDEN and st["final_outcome"] == rb.DENY
    assert st["recommended_outcome"] == rb.APPROVE
    assert s.execute("C-1", "alice", "reviewer")["final_outcome"] == rb.DENY


def test_override_to_refund_needs_senior():
    s = svc()
    submit(s, case(tracking_status="in_transit", days_past=3))      # INR-7 -> DENY
    raises(lambda: s.review("C-1", "alice", "reviewer", "override", "seen it", rb.APPROVE), "senior")
    assert s.review("C-1", "sam", "senior", "override", "seen it", rb.APPROVE)["status"] == OVERRIDDEN


def test_reject_then_resubmit_only_after_reject():
    s = svc()
    c = case(**LOST)
    submit(s, c)
    raises(lambda: submit(s, c), "active record")
    s.review("C-1", "alice", "reviewer", "reject", "evidence stale")
    assert submit(s, c)["status"] == PENDING                        # allowed after a rejection
    assert len(s.state("C-1")["history"]) == 1                       # state restarts from the new recommendation


def test_input_validation():
    s = svc()
    submit(s, case(**LOST))
    raises(lambda: s.review("C-1", " ", "reviewer", "approve"), "reviewer name")
    raises(lambda: s.review("C-1", "alice", "admin", "approve"), "role")
    raises(lambda: s.review("C-1", "alice", "reviewer", "maybe"), "approve, override or reject")
    raises(lambda: s.review("NOPE", "alice", "reviewer", "approve"), "no recommendation")


def test_audit_chain_detects_tampering():
    s = svc()
    submit(s, case(**LOST))
    s.review("C-1", "alice", "reviewer", "approve")
    assert s.verify_log() == (True, None)
    lines = s.path.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[1])
    rec["actor"] = "mallory"                                          # edit a field, keep the hash
    lines[1] = json.dumps(rec)
    s.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert s.verify_log() == (False, 2)
    # removing the first entry also breaks the chain
    s.path.write_text("\n".join(lines[1:]) + "\n", encoding="utf-8")
    assert s.verify_log()[0] is False


def test_queue_lists_only_open_cases():
    s = svc()
    submit(s, case("A", **LOST))
    submit(s, case("B", **ESC))
    s.review("A", "alice", "reviewer", "approve")
    assert [q["case_id"] for q in s.queue()] == ["B"]
    assert [q["case_id"] for q in s.queue(APPROVED)] == ["A"]


def test_cli_flow_and_refusals():
    import io
    from contextlib import redirect_stderr, redirect_stdout

    import review_cli
    s = svc()
    submit(s, case(**LOST))

    def run(*argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = review_cli.main(list(argv), svc=s)
        return code, out.getvalue(), err.getvalue()

    assert run("queue")[0] == 0 and "C-1" in run("queue")[1]
    code, _, err = run("execute", "C-1", "--reviewer", "alice")
    assert code == 1 and "not approved" in err
    code, _, err = run("override", "C-1", "--reviewer", "alice", "--outcome", "DENY")
    assert code == 1 and "reason" in err
    assert run("approve", "C-1", "--reviewer", "alice")[0] == 0
    assert run("execute", "C-1", "--reviewer", "alice")[0] == 0
    assert run("verify")[1].strip() == "audit log OK"
    assert run("show", "NOPE")[0] == 1


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"{len(fns)} approval tests passed")
