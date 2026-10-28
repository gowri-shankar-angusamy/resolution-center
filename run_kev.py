#!/usr/bin/env python3
"""Send cases to a local Kev server (POST /v1/systemone) and save raw outputs.

Standard library only, so it runs with any Python (no need for uv).

    python run_kev.py                       # plain: structured fields, no rules, dev split
    python run_kev.py --rules               # rule summary in the question instructions
    python run_kev.py --rules --derive      # also precompute day counts (Kev is weak at date arithmetic)
    python run_kev.py --with-text           # also include buyer/merchant statements and evidence lists

Never passes labels to the model. Resumable: rerun the same command and it
skips cases already saved. Each run writes runs/<name>.jsonl and runs/<name>.meta.json.
The test split is refused unless --allow-test is given (keep it sealed while tuning).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

OUTCOME_CRITERIA = {
    "APPROVE_BUYER": "Recommend a refund to the buyer",
    "DENY": "Close the case in the seller's favor",
    "REQUEST_EVIDENCE": "Evidence is insufficient; ask for more",
    "ESCALATE": "Needs a human investigator",
}

BASE_INSTRUCTIONS = "What should the dispute outcome be?"

RULES = """Recommend the outcome for this payment dispute using these rules, applied in order.
Global rules: G1 filed more than 180 days after the order date -> DENY. G2 is_duplicate is true -> DENY. G3 counterfeit claim (snad_issue counterfeit) -> ESCALATE. G4 evidence_conflict is true and order_amount is 1000 or more -> ESCALATE. G5 prior_buyer_disputes_90d is 3 or more and delivery_proof is signature or photo -> ESCALATE. G6 evidence_conflict is true -> ESCALATE.
INR (item not received): INR-1 filed before the promised delivery date, unless tracking is lost or returned_to_sender -> DENY. INR-2 tracking delivered with signature or photo proof and address_match true -> DENY. INR-3 tracking delivered but no proof or address_match false -> APPROVE_BUYER. INR-4 tracking lost or returned_to_sender -> APPROVE_BUYER. INR-5 no tracking, 7 or more days past the promised date, merchant gave no response -> APPROVE_BUYER. INR-6 no tracking, fewer than 7 days past the promised date -> REQUEST_EVIDENCE. INR-7 in transit: 14 or more days past the promised date -> APPROVE_BUYER, otherwise DENY.
SNAD (significantly not as described): SNAD-1 buyer gave no description or no photo -> REQUEST_EVIDENCE. SNAD-2 wrong_item -> APPROVE_BUYER. SNAD-3 attribute_mismatch and merchant gave no response -> APPROVE_BUYER. SNAD-4 subjective_only and the listing matches the item -> DENY. SNAD-5 damaged: packaging_evidence none -> APPROVE_BUYER, sound -> ESCALATE. SNAD-6 if the result would be APPROVE_BUYER but order_amount is 100 or more and return_status is not_shipped -> REQUEST_EVIDENCE instead.
If no rule applies -> REQUEST_EVIDENCE."""

TEXT_FIELDS = ("buyer_statement", "merchant_statement", "buyer_evidence", "merchant_evidence")
DROP_ALWAYS = ("case_id", "item")


def build_state(case: dict, derive: bool, with_text: bool) -> dict:
    state = {k: v for k, v in case.items() if k not in DROP_ALWAYS and (with_text or k not in TEXT_FIELDS)}
    if derive:
        o, f, p = (date.fromisoformat(case[k]) for k in ("order_date", "filed_date", "promised_delivery_date"))
        state["days_filed_after_order"] = (f - o).days
        state["days_filed_after_promised_date"] = (f - p).days  # negative means filed before the promised date
    return state


def build_request(case: dict, rules: bool, derive: bool, with_text: bool) -> dict:
    instructions = RULES if rules else BASE_INSTRUCTIONS
    return {
        "state": build_state(case, derive, with_text),
        "model": "kev-latest",
        "questions": {"outcome": {"type": "choice", "instructions": instructions, "criteria": OUTCOME_CRITERIA}},
    }


def http(url: str, body: dict | None = None, timeout: float = 600.0) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"content-type": "application/json"},
                                 method="GET" if body is None else "POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--split", default="dev", choices=["dev", "test"])
    ap.add_argument("--data", default="data")
    ap.add_argument("--url", default="http://127.0.0.1:8009")
    ap.add_argument("--rules", action="store_true", help="put the rulebook summary in the instructions")
    ap.add_argument("--derive", action="store_true", help="add precomputed day-count fields to the state")
    ap.add_argument("--with-text", action="store_true", help="include statements and evidence lists")
    ap.add_argument("--limit", type=int, default=0, help="only the first N cases (smoke test)")
    ap.add_argument("--name", default="", help="run name (default built from flags)")
    ap.add_argument("--tag", default="kev-0.8b-v1.0", help="model label for the run name, e.g. kev-4b-v1.0")
    ap.add_argument("--allow-test", action="store_true", help="required to run the sealed test split")
    a = ap.parse_args()

    if a.split == "test" and not a.allow_test:
        print("Refusing to run the test split. Freeze the setup on dev first, then pass --allow-test.")
        return 2

    variant = "+".join(x for x, on in (("rules", a.rules), ("derive", a.derive), ("text", a.with_text)) if on) or "plain"
    name = a.name or f"{a.tag}_{variant}_{a.split}"
    out_dir = Path("runs")
    out_dir.mkdir(exist_ok=True)
    out_path, meta_path = out_dir / f"{name}.jsonl", out_dir / f"{name}.meta.json"

    cases = [json.loads(line) for line in open(Path(a.data) / f"cases_{a.split}.jsonl", encoding="utf-8")]
    if a.limit:
        cases = cases[: a.limit]
    done = set()
    if out_path.exists():
        done = {json.loads(line)["case_id"] for line in open(out_path, encoding="utf-8") if line.strip()}
    todo = [c for c in cases if c["case_id"] not in done]
    print(f"run={name}: {len(done)} already saved, {len(todo)} to do")

    try:
        server_info = http(a.url + "/v1/models", timeout=30)
    except Exception as e:  # noqa: BLE001
        print(f"Cannot reach the Kev server at {a.url}: {e}\nStart it first (see CLAUDE.md).")
        return 1
    meta = {"name": name, "started_utc": datetime.now(timezone.utc).isoformat(), "split": a.split,
            "flags": {"rules": a.rules, "derive": a.derive, "with_text": a.with_text},
            "server_info": server_info, "url": a.url, "n_cases": len(cases)}
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    errors_in_row = 0
    with open(out_path, "a", encoding="utf-8") as f:
        for i, case in enumerate(todo, 1):
            t0 = time.time()
            try:
                resp = http(a.url + "/v1/systemone", build_request(case, a.rules, a.derive, a.with_text))
                ans = resp["answers"]["outcome"]
                rec = {"case_id": case["case_id"], "choice": ans["choice"], "probabilities": ans["probabilities"],
                       "latency_ms": resp.get("latency_ms"), "wall_s": round(time.time() - t0, 3),
                       "input_tokens": resp.get("usage", {}).get("input_tokens"), "raw": resp}
                errors_in_row = 0
            except (urllib.error.URLError, KeyError, ValueError, TimeoutError) as e:
                rec = {"case_id": case["case_id"], "error": repr(e)}
                errors_in_row += 1
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            if i % 10 == 0 or i == len(todo):
                print(f"  {i}/{len(todo)}  last wall time {rec.get('wall_s', '-')}s")
            if errors_in_row >= 5:
                print("5 errors in a row, stopping. Check the server window.")
                return 1
    print(f"done. Raw outputs: {out_path}\nScore with: python score_run.py --run {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
