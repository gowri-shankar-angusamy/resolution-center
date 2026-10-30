"""Offline tests for the persistent MCP stdio adapter (no MCP SDK required)."""
import json
import sys
from pathlib import Path

from mcp_evidence_client import MCPBackend
from mock_apis import SERVICE_FIELDS, ToolError


def test_stdio_client_discovers_and_calls_tools(tmp_path):
    server = tmp_path / "fake_mcp_server.py"
    tools = [{"name": name, "description": f"Read {name} evidence",
              "inputSchema": {"type": "object", "properties": {"case_id": {"type": "string"}},
                              "required": ["case_id"]}}
             for name in SERVICE_FIELDS]
    server.write_text("""
import json, sys
TOOLS = %s
for line in sys.stdin:
    req = json.loads(line)
    method = req.get('method')
    if 'id' not in req:
        continue
    if method == 'initialize':
        result = {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}},
                  'serverInfo': {'name': 'fake', 'version': '1'}}
    elif method == 'tools/list':
        result = {'tools': TOOLS}
    elif method == 'tools/call':
        name = req['params']['name']; cid = req['params']['arguments']['case_id']
        result = {'structuredContent': {'case_id': cid, 'service': name,
                   'evidence': {'synthetic_field': name, 'case_id': cid}}, 'isError': False}
    else:
        result = {}
    print(json.dumps({'jsonrpc':'2.0','id':req['id'],'result':result}), flush=True)
""" % json.dumps(tools), encoding="utf-8")
    with MCPBackend(server_script=server, timeout=3) as backend:
        assert {t["name"] for t in backend.list_tools()} == set(SERVICE_FIELDS)
        result = backend.call("tracking", "RC-0001")
        assert result == {"synthetic_field": "tracking", "case_id": "RC-0001"}


def test_mcp_client_rejects_unknown_tool(tmp_path):
    server = tmp_path / "fake_mcp_server.py"
    tools = [{"name": name, "description": name,
              "inputSchema": {"type": "object", "properties": {"case_id": {"type": "string"}},
                              "required": ["case_id"]}}
             for name in SERVICE_FIELDS]
    server.write_text("""
import json, sys
TOOLS = %s
for line in sys.stdin:
    req = json.loads(line)
    if 'id' not in req: continue
    method = req['method']
    result = {'protocolVersion':'2024-11-05','capabilities':{},'serverInfo':{'name':'fake','version':'1'}} if method == 'initialize' else {'tools':TOOLS}
    print(json.dumps({'jsonrpc':'2.0','id':req['id'],'result':result}), flush=True)
""" % json.dumps(tools), encoding="utf-8")
    with MCPBackend(server_script=server, timeout=3) as backend:
        try:
            backend.call("delete_case", "RC-0001")
        except ToolError as exc:
            assert "unknown evidence service" in str(exc)
        else:
            raise AssertionError("Unknown tool must be rejected")

if __name__ == "__main__":
    test_stdio_client_discovers_and_calls_tools(Path(__import__('tempfile').mkdtemp()))
    print("2 MCP evidence client tests passed")
