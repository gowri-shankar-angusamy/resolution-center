"""Local Ollama-backed planner for the Resolution Center evidence agent.

This module only chooses the next evidence service. It never executes tools,
changes a case, or decides the dispute outcome. EvidenceAgent remains
responsible for tool execution, retries, the call budget, and the trace.

The initial implementation intentionally requires the existing baseline
services to be gathered. The LLM may choose their order, but it may not stop
an investigation early just because it thinks the evidence is sufficient.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from evidence_agent import AgentState
from mock_apis import SERVICE_FIELDS

DEFAULT_MODEL = "qwen3.5:4b"
DEFAULT_HOST = "http://localhost:11434"
DEFAULT_TIMEOUT_SECONDS = 45

BASE_SERVICES = ("order", "dispute", "tracking", "merchant", "buyer_history")

SERVICE_DESCRIPTIONS = {
    "order": "Retrieve order and purchase details for the case.",
    "dispute": "Retrieve dispute type, filing details, statements, and buyer evidence.",
    "tracking": "Retrieve delivery status, delivery proof, and address-match information.",
    "merchant": "Retrieve the merchant response, statement, and evidence.",
    "buyer_history": "Retrieve the buyer's prior dispute count for the last 90 days.",
    "snad_claim": "Retrieve claim-specific evidence for a Significantly Not As Described dispute.",
}


class OllamaPlanner:
    """Select the next permitted service using a local Ollama chat model.

    Args:
        model: Local Ollama model tag, e.g. ``qwen3.5:4b``.
        host: Ollama server base URL.
        timeout: HTTP request timeout in seconds.
        think: Whether to enable model thinking. Defaults to False for this
            tool-selection task to keep responses focused and bounded.
        chat_fn: Optional test seam. If supplied, it receives the request body
            dict and returns the decoded Ollama response dict. Production code
            should leave this unset.

    Failure behavior:
        If Ollama is unavailable, returns malformed output, requests an invalid
        tool, or tries to stop before the baseline investigation is complete,
        this planner records ``__planner__`` in ``state.failed`` and returns
        None. EvidenceBundle.complete will therefore be False, allowing the
        existing policy layer to fail closed instead of treating the case as
        fully investigated.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        host: str = DEFAULT_HOST,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        think: bool = False,
        chat_fn: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
        tool_catalog: Optional[Callable[[], list[dict[str, Any]]]] = None,
    ) -> None:
        self.model = model
        self.host = host.rstrip("/")
        self.timeout = timeout
        self.think = think
        self._chat_fn = chat_fn
        # When supplied, this callable performs MCP tools/list discovery. The
        # planner exposes only discovered, allowlisted evidence tools to Qwen.
        self._tool_catalog = tool_catalog

    def plan_next(self, state: AgentState) -> Optional[str]:
        """Return one service name, or None when no eligible services remain."""
        try:
            catalog = self._discovered_catalog()
            available = self._available_services(state, catalog)
            if not available:
                return None
            response = self._chat(self._request_body(state, available, catalog))
            service = self._extract_service(response, state, available)
        except Exception as exc:  # Fail closed for network, parsing, and model errors.
            self._mark_failed(state, f"Ollama planner failed: {self._safe_error(exc)}")
            return None

        if service is None:
            self._mark_failed(
                state,
                "Ollama planner returned no tool call before required evidence services were gathered.",
            )
            return None
        return service

    def _discovered_catalog(self) -> dict[str, dict[str, Any]]:
        if self._tool_catalog is None:
            return {name: {"name": name, "description": SERVICE_DESCRIPTIONS[name],
                           "inputSchema": {"type": "object", "properties": {"case_id": {"type": "string"}},
                                           "required": ["case_id"]}}
                    for name in SERVICE_FIELDS}
        raw = self._tool_catalog()
        catalog = {}
        for item in raw:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                catalog[item["name"]] = item
            elif hasattr(item, "name"):
                catalog[item.name] = {"name": item.name,
                                      "description": getattr(item, "description", ""),
                                      "inputSchema": getattr(item, "inputSchema", {})}
        # Only known, read-only evidence tools are eligible. Discovery does not
        # grant a model permission to call arbitrary tools from an MCP server.
        return {name: spec for name, spec in catalog.items() if name in SERVICE_FIELDS}

    def _available_services(self, state: AgentState, catalog: dict[str, dict[str, Any]]) -> list[str]:
        available = [name for name in BASE_SERVICES if name in catalog and name not in state.tried]
        if (state.collected.get("case_type") == "SNAD" and "snad_claim" in catalog
                and "snad_claim" not in state.tried):
            available.append("snad_claim")
        return available

    def _request_body(self, state: AgentState, available: list[str],
                      catalog: Optional[dict[str, dict[str, Any]]] = None) -> dict[str, Any]:
        catalog = catalog or self._discovered_catalog()
        tools = []
        for service in available:
            spec = catalog.get(service, {})
            schema = spec.get("inputSchema") or {}
            properties = schema.get("properties") if isinstance(schema, dict) else None
            if not isinstance(properties, dict) or "case_id" not in properties:
                properties = {"case_id": {"type": "string", "description": "Exact application case ID."}}
            tools.append({
                "type": "function",
                "function": {
                    "name": service,
                    "description": spec.get("description") or SERVICE_DESCRIPTIONS[service],
                    "parameters": {"type": "object", "properties": properties,
                                    "required": ["case_id"]},
                },
            })

        # Only gathered evidence is sent. Gold labels, scenarios, and evaluation
        # metadata are not part of AgentState and are never added here.
        state_json = json.dumps(
            {
                "case_id": state.case_id,
                "completed_services": sorted(state.done),
                "failed_services": sorted(state.failed),
                "collected_evidence": state.collected,
                "available_services": available,
            },
            ensure_ascii=False,
            default=str,
        )
        return {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are the evidence-gathering planner for a payment-dispute review. "
                        "Choose exactly one of the supplied tools as the next evidence service. "
                        "Do not answer the dispute, make a recommendation, or invent facts. "
                        "Use the exact case_id provided in the state. Do not request tools that "
                        "are not listed. The application requires the baseline services to be "
                        "gathered; do not stop early. A SNAD-specific tool is offered only when "
                        "the gathered dispute data identifies the case as SNAD."
                    ),
                },
                {
                    "role": "user",
                    "content": "Choose the next evidence service for this state:\n" + state_json,
                },
            ],
            "tools": tools,
            "stream": False,
            "think": self.think,
            "options": {"temperature": 0},
        }

    def _chat(self, body: dict[str, Any]) -> dict[str, Any]:
        if self._chat_fn is not None:
            return self._chat_fn(body)

        request = Request(
            url=f"{self.host}/api/chat",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"Ollama HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"Cannot reach Ollama at {self.host}: {exc.reason}") from exc

    @staticmethod
    def _extract_service(
        response: dict[str, Any], state: AgentState, available: list[str]
    ) -> Optional[str]:
        message = response.get("message")
        if not isinstance(message, dict):
            raise ValueError("Ollama response is missing message object")
        calls = message.get("tool_calls") or []
        if not isinstance(calls, list):
            raise ValueError("Ollama tool_calls must be a list")
        if not calls:
            return None
        if len(calls) != 1:
            raise ValueError("Planner must request exactly one tool per turn")

        call = calls[0]
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict):
            raise ValueError("Tool call is missing function object")
        service = function.get("name")
        if not isinstance(service, str) or service not in SERVICE_FIELDS:
            raise ValueError(f"Model requested unknown service: {service!r}")
        if service not in available:
            raise ValueError(f"Model requested unavailable or repeated service: {service!r}")

        arguments = function.get("arguments")
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be a JSON object")
        if arguments.get("case_id") != state.case_id:
            raise ValueError("Tool call case_id does not match the application's case ID")
        if set(arguments) != {"case_id"}:
            raise ValueError("Tool call contains unexpected arguments")
        return service

    @staticmethod
    def _mark_failed(state: AgentState, message: str) -> None:
        # AgentState.failed is incorporated into EvidenceBundle.complete. This
        # ensures a planner failure cannot be mistaken for successful gathering.
        state.failed["__planner__"] = message[:500]

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        text = str(exc).replace("\n", " ").strip()
        return text[:350] or exc.__class__.__name__
