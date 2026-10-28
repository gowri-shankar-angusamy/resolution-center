"""Human approval workflow with an append-only, hash-chained audit log.

The log is the only storage. Every queue state is derived by replaying it, so
state and audit trail cannot disagree. Execution is a mock: it records the
action after approval and moves no money.

Flow:
  submit()   RECOMMENDED; a READY decision is also AUTO_EXECUTED (system actor)
  review()   APPROVED | OVERRIDDEN | REJECTED, by a named human
  execute()  EXECUTED, only after APPROVED or OVERRIDDEN

Rules enforced here (edit the constants only):
  * ESCALATED_TO_SENIOR cases, high-value cases and overrides to APPROVE_BUYER
    need reviewer_role == "senior".
  * Override and reject need a reason; an override must change the outcome.
  * A case can only be resubmitted after its last recommendation was REJECTED.
  * A case in a terminal state cannot be reviewed or executed again.

Single-process, file-based: appends are fsynced but there is no cross-process lock.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import policy
import rulebook as rb

DEFAULT_LOG = Path(os.environ.get("RC_AUDIT_LOG", Path(__file__).parent / "audit" / "audit_log.jsonl"))
SENIOR = "senior"
REVIEWER = "reviewer"
ROLES = (REVIEWER, SENIOR)
GENESIS = "0" * 64

# Case states (replayed from events)
PENDING = policy.PENDING
ESCALATED = policy.ESCALATED
APPROVED, OVERRIDDEN, REJECTED = "APPROVED", "OVERRIDDEN", "REJECTED"
EXECUTED, AUTO_EXECUTED = "EXECUTED", "AUTO_EXECUTED"
OPEN_STATES = {PENDING, ESCALATED}
TERMINAL = {EXECUTED, AUTO_EXECUTED}


class ApprovalError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(prev: str, body: dict) -> str:
    return hashlib.sha256((prev + json.dumps(body, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()


class ApprovalService:
    def __init__(self, log_path: Path = DEFAULT_LOG, clock: Callable[[], str] = _now):
        self.path, self.clock = Path(log_path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ---------- log ----------
    def events(self) -> list[dict]:
        if not self.path.exists():
            return []
        with open(self.path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def _append(self, case_id: str, event: str, actor: str, role: str, data: dict) -> dict:
        existing = self.events()
        prev = existing[-1]["hash"] if existing else GENESIS
        body = {"seq": len(existing) + 1, "ts": self.clock(), "case_id": case_id, "event": event,
                "actor": actor, "role": role, "data": data}
        rec = {**body, "prev_hash": prev, "hash": _hash(prev, body)}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return rec

    def verify_log(self) -> tuple[bool, Optional[int]]:
        """(ok, first_bad_seq). Detects edited, removed or reordered entries."""
        prev = GENESIS
        for n, rec in enumerate(self.events(), 1):
            body = {k: rec[k] for k in ("seq", "ts", "case_id", "event", "actor", "role", "data")}
            if rec["seq"] != n or rec["prev_hash"] != prev or rec["hash"] != _hash(prev, body):
                return False, n
            prev = rec["hash"]
        return True, None

    # ---------- state ----------
    def _case_events(self, case_id: str) -> list[dict]:
        return [e for e in self.events() if e["case_id"] == case_id]

    def state(self, case_id: str) -> Optional[dict]:
        """Current state of a case from its latest recommendation onward."""
        evs = self._case_events(case_id)
        start = max((i for i, e in enumerate(evs) if e["event"] == "RECOMMENDED"), default=None)
        if start is None:
            return None
        rec = evs[start]["data"]
        out = {"case_id": case_id, "status": rec["status"], "recommended_outcome": rec["final_outcome"],
               "final_outcome": rec["final_outcome"], "rule_id": rec["rule_id"], "amount": rec["amount"],
               "review_reasons": rec["review_reasons"], "history": []}
        for e in evs[start:]:
            out["history"].append({"seq": e["seq"], "ts": e["ts"], "event": e["event"], "actor": e["actor"]})
            if e["event"] == "REVIEWED":
                out["status"] = e["data"]["decision"]
                out["final_outcome"] = e["data"].get("override_outcome") or rec["final_outcome"]
            elif e["event"] in (EXECUTED, AUTO_EXECUTED):
                out["status"] = e["event"]
        return out

    def queue(self, status: Optional[str] = None) -> list[dict]:
        ids = list(dict.fromkeys(e["case_id"] for e in self.events()))
        states = [s for s in (self.state(i) for i in ids) if s]
        wanted = {status} if status else OPEN_STATES
        return [s for s in states if s["status"] in wanted]

    # ---------- actions ----------
    def submit(self, case: dict, decision: policy.PolicyDecision) -> dict:
        cur = self.state(decision.case_id)
        if cur and cur["status"] != REJECTED:
            raise ApprovalError(f"{decision.case_id} already has an active record ({cur['status']})")
        data = {"final_outcome": decision.final_outcome, "rule_id": decision.rule_id,
                "status": decision.status, "amount": case.get("order_amount"),
                "review_reasons": decision.review_reasons,
                "ai": decision.to_dict()["ai"], "ai_agrees": decision.ai_agrees}
        self._append(decision.case_id, "RECOMMENDED", "system", "system", data)
        if decision.status == policy.READY:
            self._append(decision.case_id, AUTO_EXECUTED, "system", "system",
                         {"action": f"mock:{decision.final_outcome}", "moved_money": False})
        return self.state(decision.case_id)

    def review(self, case_id: str, reviewer: str, role: str, decision: str, reason: str = "",
               override_outcome: Optional[str] = None) -> dict:
        if not reviewer.strip():
            raise ApprovalError("reviewer name is required")
        if role not in ROLES:
            raise ApprovalError(f"role must be one of {ROLES}")
        if decision not in ("approve", "override", "reject"):
            raise ApprovalError("decision must be approve, override or reject")
        cur = self.state(case_id)
        if cur is None:
            raise ApprovalError(f"no recommendation for {case_id}")
        if cur["status"] not in OPEN_STATES:
            raise ApprovalError(f"{case_id} is {cur['status']}, not open for review")
        if decision in ("override", "reject") and not reason.strip():
            raise ApprovalError(f"a reason is required to {decision}")

        outcome = cur["recommended_outcome"]
        if decision == "override":
            if override_outcome not in rb.OUTCOMES:
                raise ApprovalError(f"override_outcome must be one of {rb.OUTCOMES}")
            if override_outcome == outcome:
                raise ApprovalError("override must change the outcome; use approve instead")
            outcome = override_outcome
        elif override_outcome:
            raise ApprovalError("override_outcome is only valid with decision=override")

        if decision != "reject":
            needs_senior = (cur["status"] == ESCALATED
                            or (cur["amount"] or 0) >= policy.HIGH_VALUE
                            or (decision == "override" and outcome == rb.APPROVE))
            if needs_senior and role != SENIOR:
                raise ApprovalError("this case requires a senior reviewer")

        label = {"approve": APPROVED, "override": OVERRIDDEN, "reject": REJECTED}[decision]
        self._append(case_id, "REVIEWED", reviewer, role,
                     {"decision": label, "reason": reason.strip(),
                      "override_outcome": override_outcome if decision == "override" else None,
                      "recommended_outcome": cur["recommended_outcome"]})
        return self.state(case_id)

    def execute(self, case_id: str, actor: str, role: str) -> dict:
        if not actor.strip() or role not in ROLES:
            raise ApprovalError("a named actor with a valid role is required")
        cur = self.state(case_id)
        if cur is None or cur["status"] not in (APPROVED, OVERRIDDEN):
            raise ApprovalError(f"{case_id} is not approved for execution "
                                f"(status: {cur['status'] if cur else 'unknown'})")
        self._append(case_id, EXECUTED, actor, role,
                     {"action": f"mock:{cur['final_outcome']}", "moved_money": False})
        return self.state(case_id)
