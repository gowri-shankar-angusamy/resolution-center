"""Tests for the gold set inputs and gold_tools.py."""
import csv
import json
import tempfile
from collections import Counter
from pathlib import Path

import gold_tools as gt
import policy
import rulebook as rb

CASES = gt._cases()
META = [m for m in (json.loads(l) for l in open(gt.ROOT / "data/gold_meta.jsonl", encoding="utf-8"))
        if m["case_id"] in {c["case_id"] for c in CASES}]   # gold file may be a trimmed subset
LABEL_KEYS = {"gold_outcome", "gold_rule", "scenario", "is_hard", "pattern", "rulebook_outcome"}


def tmp():
    return Path(tempfile.mkdtemp())


def write_sheet(path, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["case_id", "outcome", "rule_id", "confidence", "notes"])
        w.writerows(rows)


def rulebook_sheet(flip=()):
    rows = []
    for c in CASES:
        o, r = rb.label_case(c)
        if c["case_id"] in flip:
            o = rb.DENY if o != rb.DENY else rb.APPROVE
        rows.append([c["case_id"], o, r, "3", ""])
    return rows


def test_gold_inputs_are_clean():
    ids = [c["case_id"] for c in CASES]
    assert len(ids) == len(set(ids)) and len(ids) >= 1 and all(i.startswith("GS-") for i in ids)
    for c in CASES:
        assert not LABEL_KEYS & set(c) and not policy.validate(c), c["case_id"]
    text = (gt.ROOT / "gold/cards.md").read_text(encoding="utf-8")
    assert "rulebook_outcome" not in text and "INR-2" not in text and "ESCALATE" not in text


def test_pattern_coverage_and_outcome_spread():
    assert {m["case_id"] for m in META} == {c["case_id"] for c in CASES}   # every case has a pattern
    assert {rb.label_case(c)[0] for c in CASES} == set(rb.OUTCOMES)


def test_check_catches_problems():
    d = tmp()
    s = d / "s.csv"
    write_sheet(s, [["GS-001", "MAYBE", "G9", "5", ""], ["GS-999", "DENY", "G1", "3", ""]])
    problems = "\n".join(gt.check(s))
    for expect in ("GS-001: outcome", "GS-001: rule_id", "GS-001: confidence", "GS-002: missing row", "GS-999: not a gold case"):
        assert expect in problems, expect
    write_sheet(s, rulebook_sheet())
    assert gt.check(s) == []


def test_compare_refuses_incomplete_sheet():
    d = tmp()
    s = d / "s.csv"
    write_sheet(s, rulebook_sheet()[:5])
    try:
        gt.compare(s, out=d / "dis.csv", lock=d / "lock")
    except SystemExit as e:
        assert "sheet not ready" in str(e)
    else:
        raise AssertionError("expected refusal")


def test_full_flow_with_disagreements_and_lock():
    d = tmp()
    s, dis, lock, final = d / "s.csv", d / "dis.csv", d / "lock", d / "final.jsonl"
    write_sheet(s, rulebook_sheet(flip={"GS-002", "GS-004"}))
    r = gt.compare(s, out=dis, lock=lock)
    assert r["disagreements"] == 2 and abs(r["outcome_agreement"] - (len(CASES) - 2) / len(CASES)) < 1e-9

    # freeze needs every disagreement resolved with a reason
    try:
        gt.freeze(s, dis=dis, lock=lock, out=final)
    except SystemExit as e:
        assert "needs resolution" in str(e)
    else:
        raise AssertionError("expected refusal")

    rows = list(csv.DictReader(open(dis, encoding="utf-8-sig")))
    rows[0].update(resolution="human", why="credible counter-claim, rulebook too literal")
    rows[1].update(resolution="rulebook", why="my slip, I missed the late reply rule")
    with open(dis, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)
    assert gt.freeze(s, dis=dis, lock=lock, out=final) == len(CASES)
    out = {json.loads(l)["case_id"]: json.loads(l) for l in open(final, encoding="utf-8")}
    c2, c4 = out["GS-002"], out["GS-004"]
    assert c2["gold_outcome"] == rb.APPROVE and c2["resolution"] == "human" and not c2["agrees_with_rulebook"]
    assert c4["gold_outcome"] == rb.label_case(next(c for c in CASES if c["case_id"] == "GS-004"))[0]
    assert out["GS-001"]["resolution"] == "agreed"

    # editing the sheet after compare invalidates the lock
    write_sheet(s, rulebook_sheet(flip={"GS-002", "GS-004", "GS-005"}))
    try:
        gt.freeze(s, dis=dis, lock=lock, out=final)
    except SystemExit as e:
        assert "run `compare` first" in str(e)
    else:
        raise AssertionError("expected refusal")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"{len(fns)} gold tests passed")
