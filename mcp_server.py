"""MCP server for the Resolution Center (stdio). Run: python mcp_server.py

Tools gather evidence, return a policy recommendation, fetch reviewer context and
queue recommendations for approval. There is deliberately no tool that approves,
overrides or executes anything; humans do that with review_cli.py. Dev split only.
"""
from typing import Any, Optional

from mcp.server.mcpserver import MCPServer

from rc_service import ResolutionService

server = MCPServer(
    "resolution-center",
    instructions=(
        "Payments dispute tools for INR and SNAD cases (synthetic dev data). The rulebook decides "
        "the outcome; results are recommendations that a human must approve. Typical flow: "
        "list_cases, then decide_case, then get_case_context to explain it, then submit_for_approval."
    ),
)
_service: Optional[ResolutionService] = None


def service() -> ResolutionService:
    global _service
    if _service is None:                 # built lazily so import stays cheap
        _service = ResolutionService()
    return _service


@server.tool()
def list_cases(case_type: Optional[str] = None, limit: int = 20) -> dict[str, Any]:
    """List dev case IDs. case_type may be 'INR' or 'SNAD'; limit is capped at 100."""
    return service().list_cases(case_type, limit)


@server.tool()
def gather_evidence(case_id: str) -> dict[str, Any]:
    """Collect a case's evidence from the order, tracking, dispute, merchant, buyer-history
    and SNAD-claim services. Returns the assembled case, a call trace and any failed services."""
    return service().gather_evidence(case_id)


@server.tool()
def decide_case(case_id: str) -> dict[str, Any]:
    """Gather evidence, apply the rulebook and return the outcome, the rule that fired, the
    approval status and the reasons a human must review. A recommendation only."""
    return service().decide_case(case_id)


@server.tool()
def get_case_context(case_id: str) -> dict[str, Any]:
    """Reviewer context for a case: the exact rule text that fired, related rules, guidance
    and the most similar past cases with their outcomes."""
    return service().get_case_context(case_id)


@server.tool()
def submit_for_approval(case_id: str) -> dict[str, Any]:
    """Put the case's recommendation in the human approval queue (writes an audit entry). A clean
    REQUEST_EVIDENCE is sent automatically; everything else waits for a human reviewer. This tool
    cannot approve, override or execute anything."""
    return service().submit_for_approval(case_id)


@server.tool()
def list_approval_queue(status: Optional[str] = None) -> dict[str, Any]:
    """List cases awaiting human review, or cases in a given status (e.g. APPROVED, EXECUTED)."""
    return service().list_approval_queue(status)


if __name__ == "__main__":
    server.run("stdio")
