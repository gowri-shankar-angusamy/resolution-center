"""MCP server exposing granular, read-only evidence tools.

This server is intentionally separate from mcp_server.py: it exposes the six
backend evidence lookups only. It calls MockBackend directly and never invokes
ResolutionService.gather_evidence, so the MCP path is not recursive.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mock_apis import MockBackend, SERVICE_FIELDS

ROOT = Path(__file__).parent
_cases_path = Path(os.getenv("RC_EVIDENCE_CASES_PATH", ROOT / "data/cases_dev.jsonl"))
with open(_cases_path, encoding="utf-8") as _f:
    _cases = [json.loads(line) for line in _f if line.strip()]
_backend = MockBackend(_cases)
server = MCPServer(
    "resolution-center-evidence",
    instructions=(
        "Read-only synthetic evidence tools. Call one evidence tool per request. "
        "Only use case IDs supplied by the application. These tools return evidence, "
        "not decisions. The deterministic rulebook remains authoritative."
    ),
)


def _call(service_name: str, case_id: str) -> dict[str, Any]:
    if not isinstance(case_id, str) or not case_id.strip():
        raise ValueError("case_id must be a non-empty string")
    return {"case_id": case_id, "service": service_name,
            "evidence": _backend.call(service_name, case_id)}

@server.tool()
def order(case_id: str) -> dict[str, Any]:
    """Read order item, amount, order date, and promised delivery date."""
    return _call("order", case_id)

@server.tool()
def dispute(case_id: str) -> dict[str, Any]:
    """Read dispute type, filing details, buyer statement/evidence, and conflict flags."""
    return _call("dispute", case_id)

@server.tool()
def tracking(case_id: str) -> dict[str, Any]:
    """Read tracking status, delivery proof, and address-match information."""
    return _call("tracking", case_id)

@server.tool()
def merchant(case_id: str) -> dict[str, Any]:
    """Read merchant response, statement, and evidence."""
    return _call("merchant", case_id)

@server.tool()
def buyer_history(case_id: str) -> dict[str, Any]:
    """Read buyer's prior dispute count for the last 90 days."""
    return _call("buyer_history", case_id)

@server.tool()
def snad_claim(case_id: str) -> dict[str, Any]:
    """Read claim-specific evidence for a Significantly Not As Described case."""
    return _call("snad_claim", case_id)

if __name__ == "__main__":
    server.run("stdio")
