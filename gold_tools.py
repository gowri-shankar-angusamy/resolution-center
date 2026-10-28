"""Gold set workflow: validate your labels, compare to the rulebook, freeze the result.

  python gold_tools.py check      validate gold/label_sheet.csv (no rulebook involved)
  python gold_tools.py compare    run the rulebook, write gold/disagreements.csv
  python gold_tools.py freeze     write data/gold_labels.jsonl once every disagreement has a resolution

Order matters: finish and `check` the whole sheet BEFORE `compare`, so labels are blind.
`compare` refuses to run on an incomplete or invalid sheet, and `freeze` needs the sheet
unchanged since `compare` (it stores a hash), so labels cannot be edited after seeing the rulebook.
"""
from __future__ import annotations

import csv
import hashlib
import json
import sys
from pathlib import Path

import rulebook as rb

ROOT = Path(__file__).parent
CASES = ROOT / "data/gold_cases.jsonl"
SHEET = ROOT / "gold/label_sheet.csv"
DISAGREE = ROOT / "gold/disagreements.csv"
LOCK = ROOT / "gold/.sheet_hash"
FINAL = ROOT / "data/gold_labels.jsonl"

RULE_IDS = {"G1", "G2", "G3", "G4", "G5", "G6", *(f"INR-{i}" for i in range(1, 8)),
            *(f"SNAD-{i}" for i in range(1, 7)), rb.DEFAULT_RULE, "NONE"}
RESOLUTIONS = ("human", "rulebook")   # whose label wins


def _read(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _cases(path: Path = CASES) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def _sheet_hash(path: Path) -> str:
    rows = sorted((r["case_id"], r["outcome"].strip(), r["rule_id"].strip()) for r in _read(path))
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def check(sheet: Path = SHEET, cases_path: Path = CASES) -> list[str]:
    """Problems with the sheet; empty list means it is complete and valid."""
    ids = [c["case_id"] for c in _cases(cases_path)]
    rows = {r["case_id"]: r for r in _read(sheet)}
    problems = []
    for cid in ids:
        r = rows.get(cid)
        if r is None:
            problems.append(f"{cid}: missing row")
            continue
        if r["outcome"].strip() not in rb.OUTCOMES:
            problems.append(f"{cid}: outcome must be one of {rb.OUTCOMES}, got {r['outcome']!r}")
        if r["rule_id"].strip() not in RULE_IDS:
            problems.append(f"{cid}: rule_id must be a rule ID, DEFAULT or NONE, got {r['rule_id']!r}")
        if r["confidence"].strip() not in ("1", "2", "3"):
            problems.append(f"{cid}: confidence must be 1 (unsure), 2 or 3 (certain), got {r['confidence']!r}")
    problems += [f"{cid}: not a gold case" for cid in rows if cid not in ids]
    return problems


def compare(sheet: Path = SHEET, cases_path: Path = CASES, out: Path = DISAGREE, lock: Path = LOCK) -> dict:
    problems = check(sheet, cases_path)
    if problems:
        raise SystemExit("sheet not ready:\n  " + "\n  ".join(problems))
    labels = {r["case_id"]: r for r in _read(sheet)}
    rows, agree_outcome, agree_rule = [], 0, 0
    for c in _cases(cases_path):
        h = labels[c["case_id"]]
        outcome, rule = rb.label_case(c)
        ho, hr = h["outcome"].strip(), h["rule_id"].strip()
        agree_outcome += ho == outcome
        agree_rule += hr == rule
        if ho != outcome:
            rows.append({"case_id": c["case_id"], "human_outcome": ho, "human_rule": hr,
                         "human_confidence": h["confidence"].strip(), "human_notes": h["notes"],
                         "rulebook_outcome": outcome, "rulebook_rule": rule, "resolution": "", "why": ""})
    out.parent.mkdir(exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["case_id", "human_outcome", "human_rule", "human_confidence",
                                          "human_notes", "rulebook_outcome", "rulebook_rule", "resolution", "why"])
        w.writeheader()
        w.writerows(rows)
    lock.write_text(_sheet_hash(sheet))
    n = len(labels)
    return {"n": n, "outcome_agreement": agree_outcome / n, "rule_agreement": agree_rule / n,
            "disagreements": len(rows)}


def freeze(sheet: Path = SHEET, cases_path: Path = CASES, dis: Path = DISAGREE,
           lock: Path = LOCK, out: Path = FINAL) -> int:
    if not lock.exists() or lock.read_text() != _sheet_hash(sheet):
        raise SystemExit("run `compare` first; the sheet changed (or was never compared) since then")
    resolved = {r["case_id"]: r for r in _read(dis)}
    open_ = [cid for cid, r in resolved.items()
             if r["resolution"].strip() not in RESOLUTIONS or not r["why"].strip()]
    if open_:
        raise SystemExit(f"every disagreement needs resolution ({'/'.join(RESOLUTIONS)}) and a why: {open_}")
    labels = {r["case_id"]: r for r in _read(sheet)}
    n = 0
    with open(out, "w", encoding="utf-8") as f:
        for c in _cases(cases_path):
            cid = c["case_id"]
            h, (ro, rr) = labels[cid], rb.label_case(c)
            d = resolved.get(cid)
            use_human = d is None or d["resolution"].strip() == "human"
            f.write(json.dumps({
                "case_id": cid,
                "gold_outcome": h["outcome"].strip() if use_human else ro,
                "gold_rule": h["rule_id"].strip() if use_human else rr,
                "human_outcome": h["outcome"].strip(), "human_rule": h["rule_id"].strip(),
                "human_confidence": int(h["confidence"]),
                "rulebook_outcome": ro, "rulebook_rule": rr,
                "agrees_with_rulebook": d is None,
                "resolution": d["resolution"].strip() if d else "agreed",
                "resolution_why": d["why"].strip() if d else "",
            }, ensure_ascii=False) + "\n")
            n += 1
    return n


def main(argv=None) -> int:
    cmd = (argv or sys.argv[1:] or [""])[0]
    if cmd == "check":
        problems = check()
        print("sheet OK: all cases labeled" if not problems else "\n".join(problems))
        return 1 if problems else 0
    if cmd == "compare":
        r = compare()
        print(f"{r['n']} cases | outcome agreement {r['outcome_agreement']:.0%} | rule agreement "
              f"{r['rule_agreement']:.0%} | {r['disagreements']} disagreements -> {DISAGREE}")
        return 0
    if cmd == "freeze":
        print(f"froze {freeze()} labels -> {FINAL}")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
