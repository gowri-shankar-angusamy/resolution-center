"""Local Ollama-backed decision advisor for the Resolution Center.

The advisor proposes an outcome for evaluation only. It does not execute tools,
change records, approve cases, or override the deterministic rulebook.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import policy
import rag_indexes as rag
import rulebook as rb

DEFAULT_MODEL = "qwen3.5:4b"
DEFAULT_HOST = "http://localhost:11434"
DEFAULT_TIMEOUT_SECONDS = 60


class AdvisorError(RuntimeError):
    """The local model failed to return a valid recommendation."""


@dataclass
class AdvisorResult:
    status: str
    recommendation: policy.AIRecommendation | None
    latency_ms: float
    model: str
    error: str | None = None
    details: dict[str, Any] | None = None
    advisor_mode: str = "ai_only"

    def to_dict(self) -> dict:
        recommendation = None
        if self.recommendation is not None:
            recommendation = {
                "outcome": self.recommendation.outcome,
                "confidence": self.recommendation.confidence,
                "rule_id": self.recommendation.rule_id,
                "rationale": self.recommendation.rationale,
                "source": self.recommendation.source,
            }
        return {"status": self.status, "recommendation": recommendation,
                "latency_ms": self.latency_ms, "model": self.model,
                "error": self.error, "details": self.details,
                "advisor_mode": self.advisor_mode}


class OllamaAdvisor:
    def __init__(self, model: str = DEFAULT_MODEL, host: str = DEFAULT_HOST,
                 timeout: int = DEFAULT_TIMEOUT_SECONDS,
                 chat_fn: Callable[[dict], dict] | None = None):
        if timeout <= 0:
            raise ValueError("advisor timeout must be greater than zero")
        self.model = model
        self.host = host.rstrip("/")
        self.timeout = timeout
        self.chat_fn = chat_fn

    def _chat(self, body: dict) -> dict:
        if self.chat_fn is not None:
            return self.chat_fn(body)
        req = Request(self.host + "/api/chat", data=json.dumps(body).encode("utf-8"),
                      headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(req, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise AdvisorError(f"Ollama HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise AdvisorError(f"Cannot reach Ollama at {self.host}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise AdvisorError("Ollama returned invalid JSON") from exc

    @staticmethod
    def _review_context(context: dict | None, case: dict, mode: str) -> dict:
        """Build model context for the selected experiment mode.

        The exact fired rule/final outcome are never sent. In RAG mode, explicitly
        applicable rules are added even if keyword retrieval missed them.
        """
        if mode == "ai_only":
            return {}
        if mode == "ai_explicit_policy":
            return {
                "applicable_policy_rules": rag.applicable_policy_text(case),
            }
        if mode == "ai_policy_rag":
            context = context or {}
            related = list(context.get("related_rules", []))
            seen = set(related)
            for rule_text in rag.applicable_policy_text(case):
                if rule_text not in seen:
                    related.append(rule_text)
                    seen.add(rule_text)
            return {
                "related_policy_rules": related,
                "guidance": context.get("guidance", []),
                "similar_development_cases": context.get("similar_cases", []),
            }
        raise ValueError("advisor mode must be ai_only, ai_policy_rag, or ai_explicit_policy")

    def recommend(self, case: dict, context: dict | None = None,
                  mode: str = "ai_only") -> AdvisorResult:
        started = perf_counter()
        # Limit input to case evidence; never include gold labels or evaluation metadata.
        case_input = {k: v for k, v in case.items()
                      if k not in {"gold_outcome", "gold_rule", "scenario", "is_hard",
                                   "human_outcome", "rulebook_outcome"}}
        if mode not in {"ai_only", "ai_policy_rag", "ai_explicit_policy"}:
            return AdvisorResult("error", None, round((perf_counter() - started) * 1000, 2),
                                 self.model, error=f"Unsupported advisor mode: {mode!r}", advisor_mode=mode)
        model_context = self._review_context(context, case_input, mode)
        payload = {
            "case": case_input,
            "policy_context": model_context,
            "allowed_outcomes": rb.OUTCOMES,
        }
        system = (
            "You are a dispute-resolution decision advisor. Recommend one outcome based only on the supplied case evidence "
            "and the policy context provided (which may be empty). Compare the buyer statement, merchant statement, and "
            "structured records explicitly; identify corroboration, contradictions, and what the evidence does not prove. "
            "Consider every supplied applicable policy rule, including escalation rules; do not assume a delivery scan or "
            "signature alone proves the buyer personally received the item. The deterministic rulebook remains authoritative; "
            "your answer is advisory and will be compared with it. Do not claim you executed or approved anything. "
            "Do not treat a missing field as proof. If evidence is incomplete or contradictory, prefer REQUEST_EVIDENCE or ESCALATE. "
            "Return one JSON object only, with keys: outcome, confidence, rule_id, rationale, evidence_for, evidence_against, missing_information. "
            "outcome must be exactly one of the allowed outcomes. confidence must be a number from 0 to 1. "
            "rule_id may be null if you cannot identify a rule. Keep rationale concise and evidence-grounded."
        )
        body = {
            "model": self.model,
            "stream": False,
            "think": False,
            "format": "json",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "options": {"temperature": 0},
        }
        try:
            response = self._chat(body)
            message = response.get("message") or {}
            content = message.get("content", "")
            if not isinstance(content, str) or not content.strip():
                raise AdvisorError("Ollama returned an empty advisor response")
            content = content.strip()
            if content.startswith("```"):
                content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.I)
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as exc:
                raise AdvisorError("Advisor response was not valid JSON") from exc
            if not isinstance(parsed, dict):
                raise AdvisorError("Advisor response must be a JSON object")
            outcome = parsed.get("outcome")
            if outcome not in rb.OUTCOMES:
                raise AdvisorError(f"Advisor returned unsupported outcome: {outcome!r}")
            try:
                confidence = float(parsed.get("confidence"))
            except (TypeError, ValueError) as exc:
                raise AdvisorError("Advisor confidence must be numeric") from exc
            if not 0 <= confidence <= 1:
                raise AdvisorError("Advisor confidence must be between 0 and 1")
            rule_id = parsed.get("rule_id")
            if rule_id is not None and not isinstance(rule_id, str):
                raise AdvisorError("Advisor rule_id must be a string or null")
            rationale = parsed.get("rationale", "")
            if not isinstance(rationale, str):
                raise AdvisorError("Advisor rationale must be a string")
            recommendation = policy.AIRecommendation(
                outcome=outcome, confidence=confidence, rule_id=rule_id,
                rationale=rationale[:2000], source=f"ollama:{self.model}")
            details = {k: parsed.get(k, []) for k in
                       ("evidence_for", "evidence_against", "missing_information")}
            for key, value in details.items():
                if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
                    raise AdvisorError(f"Advisor {key} must be a list of strings")
                details[key] = value[:8]
            details["input_audit"] = {
                "mode": mode,
                "case_input": case_input,
                "policy_context": model_context,
                "allowed_outcomes": rb.OUTCOMES,
                "system_prompt": system,
                "request_settings": {"model": self.model, "stream": False, "think": False,
                                     "format": "json", "temperature": 0},
                "note": "This is the logical input assembled for Ollama. No fired rule, final rulebook outcome, or gold label is included.",
            }
            return AdvisorResult("success", recommendation,
                                 round((perf_counter() - started) * 1000, 2), self.model,
                                 details=details, advisor_mode=mode)
        except AdvisorError as exc:
            return AdvisorResult("error", None, round((perf_counter() - started) * 1000, 2),
                                 self.model, error=str(exc), advisor_mode=mode)
        except Exception as exc:  # fail closed; never let an advisor failure decide a case
            return AdvisorResult("error", None, round((perf_counter() - started) * 1000, 2),
                                 self.model, error=f"Unexpected advisor error: {type(exc).__name__}: {exc}", advisor_mode=mode)
