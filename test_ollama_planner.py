"""Offline tests for OllamaPlanner. No Ollama server or model is required."""
from __future__ import annotations

from evidence_agent import AgentState
from ollama_planner import OllamaPlanner


def fake_response(name, case_id="RC-0001", extra_args=None):
    arguments = {"case_id": case_id}
    if extra_args:
        arguments.update(extra_args)
    return {
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": name, "arguments": arguments}}
            ],
        }
    }


def test_selects_allowed_tool_and_sends_think_false():
    seen = {}

    def chat(body):
        seen.update(body)
        return fake_response("tracking")

    state = AgentState("RC-0001", collected={"case_type": "INR"}, done={"order"})
    planner = OllamaPlanner(chat_fn=chat)
    assert planner.plan_next(state) == "tracking"
    assert seen["model"] == "qwen3.5:4b"
    assert seen["think"] is False
    assert "tracking" in [tool["function"]["name"] for tool in seen["tools"]]
    assert "order" not in [tool["function"]["name"] for tool in seen["tools"]]


def test_snad_tool_only_available_after_case_type_is_known():
    seen = {}

    def chat(body):
        seen.update(body)
        names = [tool["function"]["name"] for tool in body["tools"]]
        # Return a valid tool for each state so this test only checks exposure.
        return fake_response("order" if "order" in names else names[0])

    planner = OllamaPlanner(chat_fn=chat)
    planner.plan_next(AgentState("RC-0001"))
    offered = [tool["function"]["name"] for tool in seen["tools"]]
    assert "snad_claim" not in offered

    seen.clear()
    planner.plan_next(AgentState("RC-0001", collected={"case_type": "SNAD"}, done={"dispute"}))
    offered = [tool["function"]["name"] for tool in seen["tools"]]
    assert "snad_claim" in offered


def test_wrong_case_id_fails_closed():
    planner = OllamaPlanner(chat_fn=lambda body: fake_response("order", case_id="OTHER"))
    state = AgentState("RC-0001")
    assert planner.plan_next(state) is None
    assert "__planner__" in state.failed
    assert "case_id" in state.failed["__planner__"]


def test_unknown_or_repeated_service_fails_closed():
    for response_name, state in [
        ("delete_everything", AgentState("RC-0001")),
        ("order", AgentState("RC-0001", done={"order"})),
    ]:
        planner = OllamaPlanner(chat_fn=lambda body, n=response_name: fake_response(n))
        assert planner.plan_next(state) is None
        assert "__planner__" in state.failed


def test_premature_stop_fails_closed():
    planner = OllamaPlanner(chat_fn=lambda body: {"message": {"role": "assistant", "content": "done"}})
    state = AgentState("RC-0001")
    assert planner.plan_next(state) is None
    assert "__planner__" in state.failed
    assert "no tool call" in state.failed["__planner__"]


def test_model_error_fails_closed():
    def broken(_body):
        raise RuntimeError("Ollama offline")

    state = AgentState("RC-0001")
    assert OllamaPlanner(chat_fn=broken).plan_next(state) is None
    assert "Ollama offline" in state.failed["__planner__"]


def test_multiple_tool_calls_fail_closed():
    response = {
        "message": {
            "tool_calls": [
                {"function": {"name": "order", "arguments": {"case_id": "RC-0001"}}},
                {"function": {"name": "tracking", "arguments": {"case_id": "RC-0001"}}},
            ]
        }
    }
    state = AgentState("RC-0001")
    assert OllamaPlanner(chat_fn=lambda body: response).plan_next(state) is None
    assert "exactly one tool" in state.failed["__planner__"]


def test_unexpected_arguments_fail_closed():
    planner = OllamaPlanner(
        chat_fn=lambda body: fake_response("order", extra_args={"delete_case": True})
    )
    state = AgentState("RC-0001")
    assert planner.plan_next(state) is None
    assert "unexpected arguments" in state.failed["__planner__"]


def test_no_available_services_returns_none_without_error():
    state = AgentState(
        "RC-0001",
        collected={"case_type": "INR"},
        done={"order", "dispute", "tracking", "merchant", "buyer_history"},
    )
    assert OllamaPlanner(chat_fn=lambda body: {}) .plan_next(state) is None
    assert "__planner__" not in state.failed


def test_if_snad_then_claim_is_required_before_planner_can_finish():
    state = AgentState(
        "RC-0001",
        collected={"case_type": "SNAD"},
        done={"order", "dispute", "tracking", "merchant", "buyer_history"},
    )
    planner = OllamaPlanner(chat_fn=lambda body: {"message": {"content": "done"}})
    assert planner.plan_next(state) is None
    assert "__planner__" in state.failed


def test_mcp_discovered_catalog_drives_tool_schemas_and_is_allowlisted():
    seen = {}
    catalog = [
        {"name": "order", "description": "MCP order tool", "inputSchema": {
            "type": "object", "properties": {"case_id": {"type": "string"}}, "required": ["case_id"]}},
        {"name": "tracking", "description": "MCP tracking tool", "inputSchema": {
            "type": "object", "properties": {"case_id": {"type": "string"}}, "required": ["case_id"]}},
        {"name": "delete_case", "description": "must never be offered", "inputSchema": {"type": "object"}},
    ]
    def chat(body):
        seen.update(body)
        return fake_response("tracking")
    state = AgentState("RC-0001")
    planner = OllamaPlanner(chat_fn=chat, tool_catalog=lambda: catalog)
    assert planner.plan_next(state) == "tracking"
    tools = {t["function"]["name"]: t["function"] for t in seen["tools"]}
    assert set(tools) == {"order", "tracking"}
    assert tools["order"]["description"] == "MCP order tool"
    assert "case_id" in tools["order"]["parameters"]["properties"]


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"{len(tests)} Ollama planner tests passed")
