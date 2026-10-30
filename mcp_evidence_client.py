"""Small synchronous MCP stdio client adapter for the EvidenceAgent.

Uses the MCP JSON-RPC stdio framing (one JSON message per line) so the existing
synchronous EvidenceAgent can call a persistent MCP server without making its
workflow async. For production, prefer the official SDK client transport and
add process supervision/authentication appropriate to the deployment.
"""
from __future__ import annotations

import atexit
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Optional

from mock_apis import SERVICE_FIELDS, ToolError

ROOT = Path(__file__).parent

class MCPBackend:
    """Backend-compatible adapter: call(service, case_id) via MCP tools/call."""
    def __init__(self, server_script: Path = ROOT / "evidence_mcp_server.py", timeout: int = 15,
                 cases_path: Optional[Path] = None):
        self.timeout = timeout
        self._next_id = 0
        self._lock = threading.Lock()
        self._responses: queue.Queue[dict[str, Any]] = queue.Queue()
        self._closed = False
        child_env = os.environ.copy()
        if cases_path is not None:
            child_env["RC_EVIDENCE_CASES_PATH"] = str(Path(cases_path).resolve())
        self._process = subprocess.Popen(
            [sys.executable, str(server_script)], cwd=str(ROOT), env=child_env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", bufsize=1,
        )
        atexit.register(self.close)
        self._reader = threading.Thread(target=self._read_responses, daemon=True)
        self._reader.start()
        try:
            self._request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "resolution-center-evidence-client", "version": "1.0.0"},
            })
            self._notify("notifications/initialized", {})
            self._catalog = self._request("tools/list", {}).get("tools", [])
            discovered = {t.get("name") for t in self._catalog if isinstance(t, dict)}
            missing = set(SERVICE_FIELDS) - discovered
            if missing:
                raise RuntimeError(f"MCP evidence server is missing required tools: {sorted(missing)}")
        except Exception:
            self.close()
            raise

    def _read_responses(self) -> None:
        assert self._process.stdout is not None
        for line in self._process.stdout:
            try:
                msg = json.loads(line)
                if isinstance(msg, dict) and "id" in msg:
                    self._responses.put(msg)
            except json.JSONDecodeError:
                # Protocol stdout must be JSON-RPC only; ignore malformed lines
                # defensively, but request timeout will surface a broken server.
                continue
        self._responses.put({"_process_exited": True, "returncode": self._process.poll()})

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._closed or self._process.poll() is not None:
                raise RuntimeError("MCP evidence server is not running")
            self._next_id += 1
            request_id = self._next_id
            payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            assert self._process.stdin is not None
            self._process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            self._process.stdin.flush()
            # A single caller/lock means responses are consumed in request order.
            response = self._responses.get(timeout=self.timeout)
            if response.get("_process_exited"):
                raise RuntimeError("MCP evidence server exited unexpectedly")
            if response.get("id") != request_id:
                raise RuntimeError("MCP response ID did not match request")
            if "error" in response:
                error = response["error"]
                raise RuntimeError(f"MCP {method} failed: {error.get('message', error)}")
            result = response.get("result")
            if not isinstance(result, dict):
                raise RuntimeError(f"MCP {method} returned an invalid result")
            return result

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        if self._closed or self._process.poll() is not None:
            raise RuntimeError("MCP evidence server is not running")
        assert self._process.stdin is not None
        self._process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params},
                                             separators=(",", ":")) + "\n")
        self._process.stdin.flush()

    def list_tools(self) -> list[dict[str, Any]]:
        # Cached for stable schemas during a case; explicit refresh is available.
        return list(self._catalog)

    def refresh_tools(self) -> list[dict[str, Any]]:
        self._catalog = self._request("tools/list", {}).get("tools", [])
        return self.list_tools()

    def call(self, service: str, case_id: str) -> dict[str, Any]:
        if service not in SERVICE_FIELDS:
            raise ToolError(f"unknown evidence service {service!r}", transient=False)
        if service not in {t.get("name") for t in self._catalog}:
            raise ToolError(f"MCP tool {service!r} was not discovered", transient=False)
        try:
            result = self._request("tools/call", {"name": service, "arguments": {"case_id": case_id}})
        except Exception as exc:
            raise ToolError(str(exc), transient=True) from exc
        if result.get("isError"):
            contents = result.get("content") or []
            detail = " ".join(str(c.get("text", "")) for c in contents if isinstance(c, dict))
            # Invalid case / non-SNAD claim are permanent; other errors can retry.
            transient = not ("not found" in detail.lower() or "no snad claim" in detail.lower())
            raise ToolError(detail or f"MCP tool {service} failed", transient=transient)
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            payload = structured
        else:
            payload = None
            for item in result.get("content", []):
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    try:
                        decoded = json.loads(item["text"])
                        if isinstance(decoded, dict):
                            payload = decoded
                            break
                    except json.JSONDecodeError:
                        continue
        if not isinstance(payload, dict) or not isinstance(payload.get("evidence"), dict):
            raise ToolError("MCP tool returned no structured evidence object", transient=False)
        return payload["evidence"]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._process.poll() is None:
            try:
                if self._process.stdin:
                    self._process.stdin.close()
                self._process.terminate()
                self._process.wait(timeout=2)
            except Exception:
                self._process.kill()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
