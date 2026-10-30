"""Tests for the local AI advisor; all model responses are mocked."""
import json

import rulebook as rb
from ai_advisor import OllamaAdvisor


def response(payload):
    return {"message": {"content": json.dumps(payload)}}


def test_valid_recommendation_and_structured_details():
    advisor = OllamaAdvisor(chat_fn=lambda body: response({
        "outcome": rb.ESCALATE, "confidence": 0.83, "rule_id": "G4",
        "rationale": "High value and conflicting evidence need review.",
        "evidence_for": ["high-value order", "evidence conflict"],
        "evidence_against": [], "missing_information": ["senior review"],
    }))
    result = advisor.recommend({"case_id": "RC-X", "case_type": "INR", "order_amount": 1500},
                              {"final_outcome": "ESCALATE", "fired_rule": {"id": "G4"},
                               "related_rules": ["INR-2"], "guidance": ["partial evidence"],
                               "similar_cases": []})
    assert result.status == "success"
    assert result.recommendation.outcome == rb.ESCALATE
    assert result.recommendation.source == "ollama:qwen3.5:4b"
    assert result.details["evidence_for"] == ["high-value order", "evidence conflict"]


def test_rejects_invalid_outcome_and_confidence():
    bad_outcome = OllamaAdvisor(chat_fn=lambda body: response({"outcome": "REFUND", "confidence": 0.8}))
    assert bad_outcome.recommend({"case_id": "RC-X"}).status == "error"
    bad_conf = OllamaAdvisor(chat_fn=lambda body: response({"outcome": rb.DENY, "confidence": 1.5}))
    assert bad_conf.recommend({"case_id": "RC-X"}).status == "error"


def test_model_failure_is_fail_closed():
    def broken(_):
        raise OSError("offline")
    result = OllamaAdvisor(chat_fn=broken).recommend({"case_id": "RC-X"})
    assert result.status == "error" and result.recommendation is None


def test_prompt_does_not_expose_exact_fired_rule_or_final_outcome():
    captured = {}
    def fake(body):
        captured.update(body)
        return response({"outcome": rb.REQUEST, "confidence": 0.6, "rationale": "Need more information",
                         "evidence_for": [], "evidence_against": [], "missing_information": []})
    advisor = OllamaAdvisor(chat_fn=fake)
    advisor.recommend({"case_id": "RC-X", "gold_outcome": "DENY", "gold_rule": "G1", "scenario": "hidden"},
                      {"final_outcome": "ESCALATE", "fired_rule": {"id": "G4", "text": "secret exact rule"},
                       "related_rules": ["INR-2"], "guidance": ["guidance"], "similar_cases": []})
    prompt = captured["messages"][1]["content"]
    assert "gold_outcome" not in prompt and "gold_rule" not in prompt and "scenario" not in prompt
    assert "secret exact rule" not in prompt and '"final_outcome"' not in prompt


def test_ai_only_sends_no_policy_context_and_audits_exact_input():
    captured = {}
    def fake(body):
        captured.update(body)
        return response({"outcome": rb.DENY, "confidence": 0.7, "rule_id": None,
                         "rationale": "Based on case evidence.", "evidence_for": [],
                         "evidence_against": [], "missing_information": []})
    case = {"case_id": "RC-X", "case_type": "INR", "buyer_statement": "not received",
            "merchant_statement": "signed", "order_amount": 1205, "evidence_conflict": True,
            "tracking_status": "delivered", "delivery_proof": "signature", "address_match": True}
    result = OllamaAdvisor(chat_fn=fake).recommend(case, {"related_rules": ["INR-2"], "guidance": ["g"], "similar_cases": []}, mode="ai_only")
    payload = json.loads(captured["messages"][1]["content"])
    assert payload["policy_context"] == {}
    assert payload["case"]["buyer_statement"] == "not received"
    assert result.details["input_audit"]["mode"] == "ai_only"
    assert result.details["input_audit"]["policy_context"] == {}


def test_rag_mode_includes_inr2_and_g4_when_both_apply():
    captured = {}
    def fake(body):
        captured.update(body)
        return response({"outcome": rb.ESCALATE, "confidence": 0.8, "rule_id": "G4",
                         "rationale": "Conflict on a high-value case.", "evidence_for": [],
                         "evidence_against": [], "missing_information": []})
    from rag_indexes import build_policy_docs
    case = {"case_id": "RC-0085", "case_type": "INR", "order_amount": 1205,
            "order_date": "2025-12-19", "promised_delivery_date": "2025-12-26", "filed_date": "2026-01-02",
            "evidence_conflict": True, "is_duplicate": False, "tracking_status": "delivered",
            "delivery_proof": "signature", "address_match": True, "merchant_response": "provided",
            "prior_buyer_disputes_90d": 1}
    result = OllamaAdvisor(chat_fn=fake).recommend(case, {"related_rules": [], "guidance": ["guidance"], "similar_cases": []}, mode="ai_policy_rag")
    payload = json.loads(captured["messages"][1]["content"])
    rules = payload["policy_context"]["related_policy_rules"]
    assert any(text.startswith("INR-2 ") for text in rules)
    assert any(text.startswith("G4 ") for text in rules)
    assert result.advisor_mode == "ai_policy_rag"
    assert result.details["input_audit"]["policy_context"] == payload["policy_context"]


def test_explicit_policy_mode_sends_applicable_policy_only():
    captured = {}
    def fake(body):
        captured.update(body)
        return response({"outcome": rb.ESCALATE, "confidence": 0.8, "rule_id": "G4",
                         "rationale": "Escalation applies.", "evidence_for": [],
                         "evidence_against": [], "missing_information": []})
    case = {"case_id": "RC-0085", "case_type": "INR", "order_amount": 1205,
            "order_date": "2025-12-19", "promised_delivery_date": "2025-12-26", "filed_date": "2026-01-02",
            "evidence_conflict": True, "is_duplicate": False, "tracking_status": "delivered",
            "delivery_proof": "signature", "address_match": True, "merchant_response": "provided",
            "prior_buyer_disputes_90d": 1}
    OllamaAdvisor(chat_fn=fake).recommend(case, {"related_rules": ["SHOULD NOT BE USED"], "guidance": ["g"], "similar_cases": []}, mode="ai_explicit_policy")
    payload = json.loads(captured["messages"][1]["content"])
    context = payload["policy_context"]
    assert "applicable_policy_rules" in context
    assert any(text.startswith("INR-2 ") for text in context["applicable_policy_rules"])
    assert any(text.startswith("G4 ") for text in context["applicable_policy_rules"])
    assert "SHOULD NOT BE USED" not in json.dumps(context)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
    print(f"{len(tests)} AI advisor tests passed")
