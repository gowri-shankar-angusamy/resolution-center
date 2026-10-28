"""Investigation console: a local web UI over the Resolution Center. Run: python console.py

  python console.py [--port 8765] [--audit PATH]    then open http://127.0.0.1:8765

Standard library only. Binds to 127.0.0.1, checks Host/Origin on every request, requires an
X-RC-Console header on writes, and serves one request at a time so audit appends cannot race.
Case sets: dev and gold (never test). Reviewer actions here are the human approval step;
the MCP server has no equivalent tools. Reviewer names and roles are self-declared (no auth).
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

import policy
import rag_indexes as rag
import rulebook as rb
from approvals import ApprovalError, ApprovalService
from rc_service import ResolutionService

ROOT = Path(__file__).parent
PAGE = ROOT / "console" / "index.html"
CASE_SETS = {"dev": "data/cases_dev.jsonl", "gold": "data/gold_cases.jsonl"}
MAX_BODY = 1_000_000


class BadRequest(Exception):
    pass


def parse_ai(raw) -> Optional[policy.AIRecommendation]:
    """Optional simulated AI recommendation from the UI."""
    if not raw or not raw.get("outcome"):
        return None
    try:
        conf = float(raw.get("confidence", 1.0))
    except (TypeError, ValueError):
        raise BadRequest("AI confidence must be a number")
    if not 0.0 <= conf <= 1.0:
        raise BadRequest("AI confidence must be between 0 and 1")
    if raw["outcome"] not in rb.OUTCOMES:
        raise BadRequest(f"AI outcome must be one of {rb.OUTCOMES}")
    return policy.AIRecommendation(raw["outcome"], conf, source="simulated")


class ConsoleApp:
    """All console logic, independent of HTTP so it can be tested directly."""

    def __init__(self, audit_path: Optional[Path] = None):
        self.approvals = ApprovalService(audit_path) if audit_path else ApprovalService()
        self.sets = {name: ResolutionService(ROOT / path, approvals=self.approvals)
                     for name, path in CASE_SETS.items() if (ROOT / path).exists()}

    def _svc(self, set_name: str) -> ResolutionService:
        if set_name not in self.sets:
            raise BadRequest(f"unknown case set {set_name!r}; choose from {sorted(self.sets)}")
        return self.sets[set_name]

    def set_of(self, case_id: str) -> Optional[str]:
        return next((n for n, s in self.sets.items() if s.get_case(case_id)), None)

    # ---- reads ----
    def cases(self, set_name: str) -> list[dict]:
        svc = self._svc(set_name)
        rows = svc.list_cases(limit=100)["cases"]
        total = svc.list_cases(limit=1)["total"]
        if total > len(rows):                       # list_cases clamps at 100
            rows = [{"case_id": c["case_id"], "case_type": c["case_type"], "order_amount": c["order_amount"]}
                    for c in svc._cases]
        for r in rows:
            st = self.approvals.state(r["case_id"])
            r["status"] = st["status"] if st else None
        return rows

    def case(self, set_name: str, case_id: str) -> dict:
        c = self._svc(set_name).get_case(case_id)
        if c is None:
            raise BadRequest(f"case {case_id} not found in {set_name}")
        return c

    def _decide(self, svc: ResolutionService, case: dict, ai) -> dict:
        d = policy.decide(case, ai)
        ctx = None if d.rule_id == "INVALID_CASE" else rag.build_context(svc.index, case, d)
        return {"decision": d.to_dict(), "context": ctx}

    def analyze(self, set_name: str, case_id: str, ai_raw=None) -> dict:
        svc = self._svc(set_name)
        self.case(set_name, case_id)
        b = svc.agent.gather(case_id)
        out = self._decide(svc, b.to_case(), parse_ai(ai_raw))
        return {"case": b.to_case(), **out, "trace": b.trace, "failed_services": b.failed_services,
                "state": self.approvals.state(case_id)}

    def whatif(self, set_name: str, case: dict, ai_raw=None) -> dict:
        if not isinstance(case, dict):
            raise BadRequest("case must be a JSON object")
        case = {**case, "case_id": str(case.get("case_id") or "WHATIF")}
        return {**self._decide(self._svc(set_name), case, parse_ai(ai_raw)),
                "note": "What-if only. Nothing was saved or queued."}

    def queue(self, status: Optional[str]) -> dict:
        items = self.approvals.queue(status)
        for i in items:
            i["set"] = self.set_of(i["case_id"])
        return {"count": len(items), "items": items}

    def audit(self, limit: int = 100) -> dict:
        ok, bad = self.approvals.verify_log()
        events = self.approvals.events()
        return {"verified": ok, "first_bad_seq": bad, "total": len(events),
                "events": list(reversed(events[-max(1, min(limit, 500)):]))}

    # ---- writes ----
    def submit(self, set_name: str, case_id: str, ai_raw=None) -> dict:
        svc = self._svc(set_name)
        self.case(set_name, case_id)
        res = svc.submit_for_approval(case_id, parse_ai(ai_raw))
        if "error" in res:
            raise BadRequest(res["error"])
        return res

    def review(self, body: dict) -> dict:
        return self.approvals.review(str(body.get("case_id", "")), str(body.get("reviewer", "")),
                                     str(body.get("role", "")), str(body.get("decision", "")),
                                     str(body.get("reason", "")), body.get("override_outcome") or None)

    def execute(self, body: dict) -> dict:
        return self.approvals.execute(str(body.get("case_id", "")), str(body.get("actor", "")),
                                      str(body.get("role", "")))


def make_handler(app: ConsoleApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "RCConsole/1.0"

        def log_message(self, fmt, *args):          # keep the terminal quiet
            pass

        # -- guards --
        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            if host not in ("127.0.0.1", "localhost"):
                return False
            origin = self.headers.get("Origin")
            if origin:
                o = urlparse(origin)
                return (o.hostname in ("127.0.0.1", "localhost")
                        and f"{o.hostname}:{o.port}" == self.headers.get("Host"))
            return True

        def _send(self, code: int, payload, ctype="application/json; charset=utf-8"):
            body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                                        "script-src 'self' 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(body)

        def _guard(self) -> bool:
            if not self._host_ok():
                self._send(403, {"error": "forbidden host or origin"})
                return False
            return True

        def do_GET(self):
            if not self._guard():
                return
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path == "/":
                    return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
                if u.path == "/api/sets":
                    return self._send(200, sorted(app.sets))
                if u.path == "/api/cases":
                    return self._send(200, app.cases(q.get("set", "dev")))
                if u.path == "/api/case":
                    return self._send(200, app.case(q.get("set", "dev"), q.get("id", "")))
                if u.path == "/api/queue":
                    return self._send(200, app.queue(q.get("status") or None))
                if u.path == "/api/audit":
                    return self._send(200, app.audit(int(q.get("limit", 100))))
                self._send(404, {"error": "not found"})
            except (BadRequest, ApprovalError, ValueError) as e:
                self._send(400, {"error": str(e)})
            except Exception:
                self._send(500, {"error": "internal error"})

        def do_POST(self):
            if not self._guard():
                return
            if self.headers.get("X-RC-Console") != "1":
                return self._send(403, {"error": "missing X-RC-Console header"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > MAX_BODY:
                    raise BadRequest("request too large")
                body = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(body, dict):
                    raise BadRequest("body must be a JSON object")
                path, ai = urlparse(self.path).path, body.get("ai")
                if path == "/api/analyze":
                    return self._send(200, app.analyze(body.get("set", "dev"), str(body.get("case_id", "")), ai))
                if path == "/api/whatif":
                    return self._send(200, app.whatif(body.get("set", "dev"), body.get("case"), ai))
                if path == "/api/submit":
                    return self._send(200, app.submit(body.get("set", "dev"), str(body.get("case_id", "")), ai))
                if path == "/api/review":
                    return self._send(200, app.review(body))
                if path == "/api/execute":
                    return self._send(200, app.execute(body))
                self._send(404, {"error": "not found"})
            except (BadRequest, ApprovalError, json.JSONDecodeError) as e:
                self._send(400, {"error": str(e)})
            except Exception:
                self._send(500, {"error": "internal error"})

    return Handler


def serve(app: ConsoleApp, port: int = 8765) -> HTTPServer:
    return HTTPServer(("127.0.0.1", port), make_handler(app))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--audit", help="audit log path (default: audit/audit_log.jsonl or RC_AUDIT_LOG)")
    a = ap.parse_args()
    app = ConsoleApp(Path(a.audit) if a.audit else None)
    httpd = serve(app, a.port)
    print(f"Resolution Center console: http://127.0.0.1:{httpd.server_address[1]}   (Ctrl+C to stop)")
    print(f"Audit log: {app.approvals.path}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
