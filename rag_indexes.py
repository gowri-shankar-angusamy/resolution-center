"""Builds the three Resolution Center indexes and the reviewer context.

  policy      one chunk per rule (G1-G6, INR-1..7, SNAD-1..6), from the rulebook docx
  guidance    outcome definitions, precedence, and the hard-case patterns
  precedents  past resolved cases: case fields plus the resolved outcome and rule

Precedents come from the dev split only. Callers must pass `exclude_case_id` so a
case never retrieves itself. Precedent documents carry only `outcome` and `rule`
as metadata (never scenario or is_hard). The test split is not read here.

`build_context` is for the human reviewer console, not for deciding. The rule that
fired is looked up exactly by ID; retrieval supplies the surrounding material.
"""
from __future__ import annotations

import html
import json
import re
import zipfile
from datetime import date
from pathlib import Path
from typing import Optional

from retrieval import BM25Index, Document, Hit, MultiIndex

ROOT = Path(__file__).parent
DOCX = ROOT / "Resolution Center — Dispute Rulebook v0.1.docx"
RULE_LINE = re.compile(r"^((?:G|INR-|SNAD-)\d)\s+(.*)$")


def _paragraphs(docx: Path) -> list[str]:
    xml = zipfile.ZipFile(docx).read("word/document.xml").decode("utf-8")
    paras = re.sub(r"<[^>]+>", "", xml.replace("</w:p>", "\n")).split("\n")
    return [html.unescape(p).strip() for p in paras if p.strip()]


def build_policy_docs(docx: Path = DOCX) -> list[Document]:
    docs = []
    for p in _paragraphs(docx):
        m = RULE_LINE.match(p)
        if m:
            docs.append(Document(m.group(1), p, {"kind": "rule"}))
    return docs


def build_guidance_docs(docx: Path = DOCX) -> list[Document]:
    paras = _paragraphs(docx)
    docs: list[Document] = []

    def section(start: str, stop: str) -> list[str]:
        i = next(n for n, p in enumerate(paras) if p.startswith(start))
        j = next(n for n, p in enumerate(paras) if n > i and p.startswith(stop))
        return paras[i + 1:j]

    outcomes = section("2. Outcomes", "3. Case fields")
    body = [p for p in outcomes if p not in ("Outcome", "Meaning")]
    for name, meaning in zip(body[0::2], body[1::2]):
        if name.isupper():
            docs.append(Document(f"outcome:{name}", f"{name}: {meaning}", {"kind": "outcome"}))
    docs.append(Document("precedence", " ".join(section("7. Precedence", "8. Hard cases")),
                         {"kind": "precedence"}))
    for n, p in enumerate(section("8. Hard cases", "9. Open decisions")):
        if not p.startswith("Suggested mix"):
            docs.append(Document(f"hardcase:{n}", p, {"kind": "hard_case"}))
    return docs


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(a) - date.fromisoformat(b)).days


def render_case(case: dict, with_text: bool = True) -> str:
    """Plain-text rendering of a case, used for both indexing and querying so the
    vocabulary matches. Only case input fields are used."""
    bits = [case["case_type"], case.get("item", ""),
            f"tracking {case['tracking_status']}", f"delivery proof {case['delivery_proof']}",
            f"address match {case['address_match']}", f"merchant response {case['merchant_response']}",
            f"return {case.get('return_status', '')}"]
    amount = case["order_amount"]
    if amount >= 1000:
        bits.append("high value order")
    if _days(case["filed_date"], case["order_date"]) > 180:
        bits.append("filed outside window")
    past = _days(case["filed_date"], case["promised_delivery_date"])
    bits.append("filed before promised date" if past < 0 else
                "past promised date by 14 or more days" if past >= 14 else
                "past promised date by 7 or more days" if past >= 7 else
                "past promised date by under 7 days")
    if case.get("is_duplicate"):
        bits.append("duplicate")
    if case.get("evidence_conflict"):
        bits.append("evidence conflict contradict")
    if case.get("prior_buyer_disputes_90d", 0) >= 3:
        bits.append("several prior disputes abuse signal")
    if case["case_type"] == "SNAD":
        bits.append(f"issue {case.get('snad_issue', '')} packaging {case.get('packaging_evidence', '')}")
        if not case.get("buyer_photo_provided") or not case.get("buyer_description_provided"):
            bits.append("missing claim evidence")
        if case.get("listing_matches_item"):
            bits.append("listing matches item")
    if with_text:
        bits += [case.get("buyer_statement", ""), case.get("merchant_statement", ""),
                 " ".join(case.get("buyer_evidence", [])), " ".join(case.get("merchant_evidence", []))]
    return " ".join(str(b) for b in bits if b)


def build_precedent_docs(cases_path: Path, labels_path: Path) -> list[Document]:
    labels = {}
    with open(labels_path, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            labels[r["case_id"]] = (r["gold_outcome"], r["gold_rule"])
    docs = []
    with open(cases_path, encoding="utf-8") as f:
        for line in f:
            c = json.loads(line)
            outcome, rule = labels[c["case_id"]]
            docs.append(Document(c["case_id"], render_case(c), {"outcome": outcome, "rule": rule}))
    return docs


def build_multi_index(dev: bool = True) -> MultiIndex:
    # Only the dev split is supported on purpose.
    assert dev, "precedents are built from the dev split only"
    return MultiIndex([
        BM25Index("policy", build_policy_docs()),
        BM25Index("guidance", build_guidance_docs()),
        BM25Index("precedents", build_precedent_docs(ROOT / "data/cases_dev.jsonl",
                                                     ROOT / "data/labels_dev.jsonl")),
    ])


def retrieve(mi: MultiIndex, case: dict, k: int = 3) -> dict[str, list[Hit]]:
    """Per-index retrieval for one case. The case itself is excluded from precedents."""
    structured = render_case(case, with_text=False)
    full = render_case(case, with_text=True)
    return mi.search(
        {"policy": structured, "guidance": structured, "precedents": full},
        k={"policy": k, "guidance": 2, "precedents": k},
        exclude_ids=frozenset({case.get("case_id", "")}),
    )


def build_context(mi: MultiIndex, case: dict, decision, k: int = 3) -> dict:
    """Reviewer context for a PolicyDecision: the rule that fired (exact lookup),
    related rules, guidance and similar past cases."""
    results = retrieve(mi, case, k)
    policy_ix = mi.indexes["policy"]
    fired: Optional[Document] = next((d for d in policy_ix.docs if d.doc_id == decision.rule_id), None)
    return {
        "case_id": case.get("case_id"),
        "final_outcome": decision.final_outcome,
        "fired_rule": {"id": decision.rule_id, "text": fired.text if fired else None},
        "related_rules": [h.doc.text for h in results["policy"] if h.doc.doc_id != decision.rule_id],
        "guidance": [h.doc.text for h in results["guidance"]],
        "similar_cases": [{"case_id": h.doc.doc_id, "outcome": h.doc.meta["outcome"],
                           "rule": h.doc.meta["rule"], "score": round(h.score, 2)}
                          for h in results["precedents"]],
    }


def applicable_policy_rule_ids(case: dict) -> list[str]:
    """Return policy rules whose conditions appear applicable from case fields.

    This is context selection only, not a second decision engine: rulebook.py
    remains authoritative. Multiple rules can be returned when their conditions
    overlap (for example INR-2 and G4 on a high-value INR case with conflicting
    delivery evidence).
    """
    ids: list[str] = []
    try:
        filed = date.fromisoformat(case["filed_date"])
        order_date = date.fromisoformat(case["order_date"])
        promised = date.fromisoformat(case["promised_delivery_date"])
    except (KeyError, TypeError, ValueError):
        filed = order_date = promised = None

    amount = case.get("order_amount", 0)
    conflict = bool(case.get("evidence_conflict", False))
    proof = case.get("delivery_proof")
    tracking = case.get("tracking_status")
    case_type = case.get("case_type")

    if filed and order_date and (filed - order_date).days > 180:
        ids.append("G1")
    if case.get("is_duplicate") is True:
        ids.append("G2")
    if case_type == "SNAD" and case.get("snad_issue") == "counterfeit":
        ids.append("G3")
    if conflict and isinstance(amount, (int, float)) and amount >= 1000:
        ids.append("G4")
    if case.get("prior_buyer_disputes_90d", 0) >= 3 and proof in ("signature", "photo"):
        ids.append("G5")
    if conflict:
        ids.append("G6")

    if case_type == "INR":
        if filed and promised and (filed - promised).days < 0 and tracking not in ("lost", "returned_to_sender"):
            ids.append("INR-1")
        if tracking == "delivered" and proof in ("signature", "photo") and case.get("address_match") is True:
            ids.append("INR-2")
        if tracking == "delivered" and (proof == "none" or case.get("address_match") is False):
            ids.append("INR-3")
        if tracking in ("lost", "returned_to_sender"):
            ids.append("INR-4")
        if filed and promised and tracking == "none" and (filed - promised).days >= 7 and case.get("merchant_response") == "none":
            ids.append("INR-5")
        if filed and promised and tracking == "none" and 0 <= (filed - promised).days < 7:
            ids.append("INR-6")
        if tracking == "in_transit":
            ids.append("INR-7")

    if case_type == "SNAD":
        if not case.get("buyer_description_provided") or not case.get("buyer_photo_provided"):
            ids.append("SNAD-1")
        issue = case.get("snad_issue")
        if issue == "wrong_item":
            ids.append("SNAD-2")
        if issue == "attribute_mismatch" and case.get("merchant_response") == "none":
            ids.append("SNAD-3")
        if issue == "subjective_only" and case.get("listing_matches_item") is True:
            ids.append("SNAD-4")
        if issue == "damaged":
            ids.append("SNAD-5")
        if isinstance(amount, (int, float)) and amount >= 100 and case.get("return_status") == "not_shipped":
            ids.append("SNAD-6")

    # Keep document order stable and avoid duplicates.
    return list(dict.fromkeys(ids))


def applicable_policy_text(case: dict, docx: Path = DOCX) -> list[str]:
    """Return exact policy-document paragraphs for all apparently applicable rules."""
    wanted = set(applicable_policy_rule_ids(case))
    return [doc.text for doc in build_policy_docs(docx) if doc.doc_id in wanted]
