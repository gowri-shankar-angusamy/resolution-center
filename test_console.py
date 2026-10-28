"""Tests for console.py (logic and HTTP layer). Run with `python test_console.py`."""
import http.client
import json
import tempfile
import threading
from pathlib import Path

import console
import policy
import rulebook as rb

TMP = Path(tempfile.mkdtemp())
APP = console.ConsoleApp(TMP / "audit.jsonl")
LABEL_KEYS = {"gold_outcome", "gold_rule", "scenario", "is_hard", "human_outcome", "rulebook_outcome"}


def raises_bad(fn, text=""):
    try:
        fn()
    except (console.BadRequest, Exception) as e:
        assert text in str(e), str(e)
        return
    raise AssertionError("expected an error")


def first_id(set_name="dev", case_type=None):
    return next(c["case_id"] for c in APP.cases(set_name) if case_type in (None, c["case_type"]))


def test_case_sets_are_dev_and_gold_only():
    assert set(APP.sets) == {"dev", "gold"}
    raises_bad(lambda: APP.cases("test"), "unknown case set")
    assert len(APP.cases("dev")) == 90 and len(APP.cases("gold")) >= 1


def test_cases_expose_inputs_only():
    for name in APP.sets:
        c = APP.case(name, first_id(name))
        assert not LABEL_KEYS & set(c)
    raises_bad(lambda: APP.case("dev", "NOPE"), "not found")


def test_analyze_returns_decision_context_and_trace():
    cid = first_id("dev")
    a = APP.analyze("dev", cid)
    assert a["decision"]["final_outcome"] in rb.OUTCOMES and a["context"]["fired_rule"]["id"] == a["decision"]["rule_id"]
    assert a["trace"] and not a["failed_services"] and a["state"] is None
    assert not LABEL_KEYS & set(a["case"])


def test_simulated_ai_disagreement_flows_through():
    cid = first_id("dev")
    truth = APP.analyze("dev", cid)["decision"]["final_outcome"]
    other = next(o for o in rb.OUTCOMES if o != truth)
    d = APP.analyze("dev", cid, {"outcome": other, "confidence": "0.9"})["decision"]
    assert d["ai_agrees"] is False and any("AI recommended" in r for r in d["review_reasons"])
    raises_bad(lambda: APP.analyze("dev", cid, {"outcome": "NOPE"}), "AI outcome")
    raises_bad(lambda: APP.analyze("dev", cid, {"outcome": truth, "confidence": "2"}), "between 0 and 1")
    raises_bad(lambda: APP.analyze("dev", cid, {"outcome": truth, "confidence": "abc"}), "number")


def test_whatif_changes_outcome_and_writes_nothing():
    c = APP.case("dev", first_id("dev", "INR"))
    before = len(APP.approvals.events())
    base = APP.whatif("dev", c)["decision"]
    edited = {**c, "is_duplicate": True}                         # G2 -> DENY
    res = APP.whatif("dev", edited)
    assert res["decision"]["rule_id"] == "G2" and res["decision"]["final_outcome"] == rb.DENY
    assert base["rule_id"] != "G2"
    assert len(APP.approvals.events()) == before
    broken = APP.whatif("dev", {"case_id": "X", "case_type": "INR"})
    assert broken["decision"]["rule_id"] == "INVALID_CASE" and broken["context"] is None
    raises_bad(lambda: APP.whatif("dev", [1, 2]), "JSON object")


def test_submit_review_execute_flow_and_queue_sets():
    cid = next(c["case_id"] for c in APP.cases("gold")
               if APP.analyze("gold", c["case_id"])["decision"]["status"] == policy.PENDING)
    assert APP.submit("gold", cid)["status"] == policy.PENDING
    raises_bad(lambda: APP.submit("gold", cid), "active record")
    q = APP.queue(None)
    assert any(i["case_id"] == cid and i["set"] == "gold" for i in q["items"])
    raises_bad(lambda: APP.review({"case_id": cid, "reviewer": "", "role": "reviewer", "decision": "approve"}), "name")
    st = APP.review({"case_id": cid, "reviewer": "alice", "role": "senior", "decision": "approve"})
    assert st["status"] == "APPROVED"
    assert APP.execute({"case_id": cid, "actor": "alice", "role": "senior"})["status"] == "EXECUTED"
    a = APP.audit()
    assert a["verified"] and a["total"] >= 3 and a["events"][0]["event"] == "EXECUTED"
    assert next(c for c in APP.cases("gold") if c["case_id"] == cid)["status"] == "EXECUTED"


# ---------------- HTTP layer ----------------
def start():
    httpd = console.serve(APP, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def call(port, method, path, body=None, headers=None, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    h = {"Host": host or f"127.0.0.1:{port}", **(headers or {})}
    data = json.dumps(body).encode() if body is not None else None
    if data:
        h["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=h)
    r = conn.getresponse()
    raw = r.read()
    conn.close()
    return r.status, r.getheaders(), raw


def test_http_pages_reads_and_guards():
    httpd, port = start()
    try:
        s, hdrs, body = call(port, "GET", "/")
        assert s == 200 and b"Resolution Center" in body
        assert dict(hdrs)["Content-Security-Policy"].startswith("default-src 'self'")
        s, _, body = call(port, "GET", "/api/sets")
        assert json.loads(body) == ["dev", "gold"]
        s, _, body = call(port, "GET", "/api/cases?set=gold")
        assert s == 200 and json.loads(body)
        assert call(port, "GET", "/api/cases?set=test")[0] == 400
        assert call(port, "GET", "/api/nothing")[0] == 404

        # host / origin guards
        assert call(port, "GET", "/api/sets", host="evil.example.com")[0] == 403
        assert call(port, "GET", "/api/sets", headers={"Origin": "http://evil.example.com"})[0] == 403
        assert call(port, "GET", "/api/sets", headers={"Origin": f"http://127.0.0.1:{port}"})[0] == 200

        # writes need the custom header, valid JSON and a JSON object
        cid = first_id("dev")
        assert call(port, "POST", "/api/analyze", {"set": "dev", "case_id": cid})[0] == 403
        ok = {"X-RC-Console": "1"}
        s, _, body = call(port, "POST", "/api/analyze", {"set": "dev", "case_id": cid}, ok)
        assert s == 200 and "decision" in json.loads(body)
        assert call(port, "POST", "/api/analyze", {"set": "dev", "case_id": "NOPE"}, ok)[0] == 400
        assert call(port, "POST", "/api/review", {"case_id": "NOPE", "reviewer": "a", "role": "reviewer",
                                                  "decision": "approve"}, ok)[0] == 400
        s, _, _ = call(port, "POST", "/api/analyze", None, {**ok, "Content-Type": "application/json"})
        assert s in (200, 400)
        assert call(port, "GET", "/api/audit")[0] == 200
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_page_has_no_inner_html_for_data():
    html = (console.ROOT / "console/index.html").read_text(encoding="utf-8")
    assert "innerHTML" not in html and "document.write" not in html and "eval(" not in html


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"{len(fns)} console tests passed")
