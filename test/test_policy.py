"""Tests for policy.py. Run with `python test_policy.py` or `pytest`."""
import policy
import rulebook as rb
from policy import AIRecommendation, decide
from test_rulebook import mk


def case(case_type="INR", **kw):
    c = mk(case_type, **kw)
    c.setdefault("case_id", "T-1")
    return c


def ai(outcome, conf=0.95):
    return AIRecommendation(outcome, conf)


def test_rules_override_ai():
    # INR-4 (lost) -> APPROVE; AI says DENY: rulebook wins, human reviews
    d = decide(case(tracking_status="lost"), ai(rb.DENY))
    assert d.final_outcome == rb.APPROVE and d.rule_id == "INR-4"
    assert d.ai_agrees is False and d.status == policy.PENDING
    assert any("AI recommended" in r for r in d.review_reasons)


def test_refund_always_needs_human():
    d = decide(case(tracking_status="lost"), ai(rb.APPROVE))
    assert d.ai_agrees and d.status == policy.PENDING and d.requires_human


def test_request_evidence_auto_when_clean():
    c = case(tracking_status="none", days_past=3)   # INR-6
    d = decide(c, ai(rb.REQUEST))
    assert (d.final_outcome, d.rule_id) == (rb.REQUEST, "INR-6")
    assert d.status == policy.READY and not d.requires_human


def test_request_evidence_held_on_low_confidence_or_disagreement():
    c = case(tracking_status="none", days_past=3)
    assert decide(c, ai(rb.REQUEST, 0.4)).status == policy.PENDING
    assert decide(c, ai(rb.DENY)).status == policy.PENDING


def test_escalate_goes_to_senior():
    d = decide(case("SNAD", snad_issue="counterfeit"), ai(rb.ESCALATE))
    assert d.status == policy.ESCALATED and d.rule_id == "G3"


def test_no_ai_still_decides():
    d = decide(case(tracking_status="lost"))
    assert d.final_outcome == rb.APPROVE and d.ai_agrees is None


def test_high_value_flagged():
    d = decide(case(tracking_status="lost", order_amount=2000), ai(rb.APPROVE))
    assert any("high-value" in r for r in d.review_reasons)


def test_unknown_ai_outcome():
    d = decide(case(tracking_status="lost"), ai("REFUND_EVERYTHING"))
    assert d.ai_agrees is False and d.status == policy.PENDING


def test_invalid_case_escalates():
    d = decide({"case_id": "X", "case_type": "CHARGEBACK"})
    assert d.rule_id == "INVALID_CASE" and d.status == policy.ESCALATED


def test_dev_cases_never_crash():
    import json
    with open("../data/cases_dev.jsonl") as f:
        for line in f:
            c = json.loads(line)
            d = decide(c)
            assert d.final_outcome in rb.OUTCOMES


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"{len(fns)} policy tests passed")
