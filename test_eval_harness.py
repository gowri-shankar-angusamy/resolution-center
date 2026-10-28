"""Tests for eval_harness.py."""
import json
import tempfile
from pathlib import Path

import eval_harness as eh
import rulebook as rb

DEV_CASES, DEV_LABELS, DEV_GROUPS = eh.load_labelset("dev")
GOLD_CASES, GOLD_LABELS, GOLD_GROUPS = eh.load_labelset("gold")


def labels(pairs):
    """Toy label set: [(case_id, gold_outcome)]."""
    return {cid: {"gold_outcome": g, "gold_rule": "X"} for cid, g in pairs}


def preds(pairs, conf=None):
    return [{"case_id": cid, "outcome": o, "confidence": conf} for cid, o in pairs]


def test_rulebook_is_perfect_on_dev_and_matches_rule_citation():
    res = eh.score(eh.predict_rulebook(DEV_CASES), DEV_LABELS, DEV_GROUPS)
    assert res["n"] == len(DEV_CASES) and res["accuracy"] == 1.0 and res["rule_citation_accuracy"] == 1.0
    assert res["wrong_refund"] == res["wrong_deny"] == res["missed_escalation"] == 0
    assert set(res["breakdown"]) == {"gold rule", "difficulty"}


def test_costly_error_definitions_match_score_run():
    gold = labels([("a", rb.DENY), ("b", rb.APPROVE), ("c", rb.ESCALATE), ("d", rb.REQUEST)])
    p = preds([("a", rb.APPROVE), ("b", rb.DENY), ("c", rb.APPROVE), ("d", rb.ESCALATE)])
    res = eh.score(p, gold, lambda r: {})
    assert res["wrong_refund"] == 2          # a (gold DENY) and c (gold ESCALATE) were refunded
    assert res["wrong_deny"] == 1            # b: DENY where gold was APPROVE_BUYER
    assert res["missed_escalation"] == 1     # c auto-decided
    assert res["accuracy"] == 0


def test_escalation_metrics():
    gold = labels([("a", rb.ESCALATE), ("b", rb.ESCALATE), ("c", rb.DENY), ("d", rb.DENY)])
    p = preds([("a", rb.ESCALATE), ("b", rb.DENY), ("c", rb.ESCALATE), ("d", rb.REQUEST)])
    e = eh.score(p, gold, lambda r: {})["escalation"]
    assert e["precision"] == 0.5 and e["recall"] == 0.5 and e["over_escalated"] == 1
    assert e["routed_to_human_rate"] == 0.75 and e["auto_decided_rate"] == 0.25


def test_missing_and_invalid_predictions_are_counted_not_scored():
    gold = labels([("a", rb.DENY), ("b", rb.DENY), ("c", rb.DENY)])
    p = preds([("a", rb.DENY), ("b", "BOGUS")])
    res = eh.score(p, gold, lambda r: {})
    assert res["n"] == 1 and sorted(res["missing"]) == ["b", "c"]


def test_wilson_interval():
    lo, hi = eh.wilson(8, 10)
    assert 0.44 < lo < 0.50 and 0.93 < hi < 0.96
    assert eh.wilson(0, 0) == (0.0, 0.0)
    lo, hi = eh.wilson(10, 10)
    assert hi == 1.0 and lo < 0.8


def test_calibration_bins():
    gold = labels([("a", rb.DENY), ("b", rb.DENY)])
    res = eh.score(preds([("a", rb.DENY), ("b", rb.APPROVE)], conf=0.9), gold, lambda r: {})
    assert abs(res["ece"] - 0.4) < 1e-9          # confidence 0.9, accuracy 0.5


def test_loads_kev_run_format():
    d = Path(tempfile.mkdtemp()) / "run.jsonl"
    d.write_text("\n".join(json.dumps(r) for r in [
        {"case_id": "a", "choice": "DENY", "probabilities": {"APPROVE_BUYER": 0.1, "DENY": 0.6, "REQUEST_EVIDENCE": 0.2, "ESCALATE": 0.1}},
        {"case_id": "b", "error": "timeout"},
    ]), encoding="utf-8")
    out = eh.load_predictions(d)
    assert len(out) == 1 and out[0]["outcome"] == "DENY" and out[0]["confidence"] == 0.6


def test_existing_kev_run_scores_on_dev():
    run = Path(eh.ROOT / "runs/kev-0.8b-v1.0_rules_dev.jsonl")
    if not run.exists():
        return
    res = eh.score(eh.load_predictions(run), DEV_LABELS, DEV_GROUPS)
    assert abs(res["accuracy"] - 0.5) < 1e-9       # same as score_run.py's saved summary


def test_gold_breakdowns_and_rulebook_gap_cases():
    res = eh.score(eh.predict_rulebook(GOLD_CASES), GOLD_LABELS, GOLD_GROUPS)
    gaps = res["breakdown"]["rulebook"]
    assert all(v["accuracy"] == 0.0 for k, v in gaps.items() if k == "gap:human")   # human overruled the rulebook
    assert all(v["accuracy"] == 1.0 for k, v in gaps.items() if k != "gap:human")
    assert eh.score(eh.predict_knn(GOLD_CASES), GOLD_LABELS, GOLD_GROUPS)["n"] == len(GOLD_CASES)


def test_test_split_is_unavailable_and_predictors_see_no_labels():
    for name in ("test", "labels_test"):
        try:
            eh.load_labelset(name)
        except SystemExit:
            continue
        raise AssertionError("test split must not load")
    banned = {"gold_outcome", "gold_rule", "scenario", "is_hard", "human_outcome", "rulebook_outcome"}
    assert not any(banned & set(c) for c in GOLD_CASES + DEV_CASES)


def test_report_flags_small_n():
    res = eh.score(eh.predict_rulebook(GOLD_CASES), GOLD_LABELS, GOLD_GROUPS)
    text = eh.format_report("rulebook", "gold", res)
    assert "is small" in text and "95% CI" in text and "Escalation as a first-class output" in text


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"{len(fns)} eval harness tests passed")
