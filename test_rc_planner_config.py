"""Tests for choosing the evidence planner without calling Ollama."""
from __future__ import annotations

import os

from evidence_agent import RulePlanner
from ollama_planner import OllamaPlanner
from ai_advisor import OllamaAdvisor
import rc_service
from rc_service import build_planner


def test_defaults_to_rule_planner(monkeypatch):
    monkeypatch.delenv("RC_PLANNER", raising=False)
    assert isinstance(build_planner(), RulePlanner)


def test_environment_can_select_ollama(monkeypatch):
    monkeypatch.setenv("RC_PLANNER", "ollama")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3.5:4b")
    monkeypatch.setenv("OLLAMA_HOST", "http://localhost:11434")
    planner = build_planner()
    assert isinstance(planner, OllamaPlanner)
    assert planner.model == "qwen3.5:4b"
    assert planner.host == "http://localhost:11434"
    assert planner.think is False


def test_explicit_mode_overrides_environment(monkeypatch):
    monkeypatch.setenv("RC_PLANNER", "ollama")
    assert isinstance(build_planner("rule"), RulePlanner)




def test_resolution_service_uses_selected_planner(monkeypatch):
    monkeypatch.setattr(rc_service.rag, "build_multi_index", lambda: {"test": True})
    service = rc_service.ResolutionService(planner_mode="ollama", approvals=object())
    assert isinstance(service.agent.planner, OllamaPlanner)
    assert service.planner_mode == "ollama"


def test_invalid_mode_is_rejected(monkeypatch):
    monkeypatch.setenv("RC_PLANNER", "magic")
    try:
        build_planner()
    except ValueError as exc:
        assert "choose 'rule' or 'ollama'" in str(exc)
    else:
        raise AssertionError("Invalid planner mode should fail clearly")


def test_invalid_timeout_is_rejected(monkeypatch):
    monkeypatch.setenv("OLLAMA_TIMEOUT", "fast")
    try:
        build_planner("ollama")
    except ValueError as exc:
        assert "OLLAMA_TIMEOUT must be an integer" in str(exc)
    else:
        raise AssertionError("Invalid timeout should fail clearly")


def test_advisor_defaults_on_for_ollama_planner(monkeypatch):
    monkeypatch.setenv("RC_PLANNER", "ollama")
    monkeypatch.delenv("RC_AI_ADVISOR", raising=False)
    assert isinstance(rc_service.build_advisor(), OllamaAdvisor)


def test_advisor_defaults_off_for_rule_planner(monkeypatch):
    monkeypatch.setenv("RC_PLANNER", "rule")
    monkeypatch.delenv("RC_AI_ADVISOR", raising=False)
    assert rc_service.build_advisor() is None


def test_advisor_can_be_disabled_explicitly(monkeypatch):
    monkeypatch.setenv("RC_PLANNER", "ollama")
    monkeypatch.setenv("RC_AI_ADVISOR", "none")
    assert rc_service.build_advisor() is None


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        # Tiny local substitute for pytest's monkeypatch fixture, so this file
        # can also be run directly with the standard library only.
        class Patch:
            def __init__(self): self.old = {}
            def setenv(self, key, value):
                self.old.setdefault(key, os.environ.get(key))
                os.environ[key] = value
            def delenv(self, key, raising=True):
                self.old.setdefault(key, os.environ.get(key))
                os.environ.pop(key, None)
            def setattr(self, obj, name, value):
                key = (obj, name)
                self.old.setdefault(key, getattr(obj, name))
                setattr(obj, name, value)
            def undo(self):
                for key, value in self.old.items():
                    if isinstance(key, tuple):
                        obj, name = key
                        setattr(obj, name, value)
                    elif value is None: os.environ.pop(key, None)
                    else: os.environ[key] = value
        patch = Patch()
        # The direct-run path calls tests with their monkeypatch parameter.
        try:
            test(patch)
        finally:
            patch.undo()
    print(f"{len(tests)} planner configuration tests passed")
