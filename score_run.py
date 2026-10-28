#!/usr/bin/env python3
"""Score a saved run against gold labels. Standard library only.

    python score_run.py --run runs/kev-0.8b-v1.0_plain_dev.jsonl

Predictions come from argmax of the returned probabilities; confidence is the
top probability (not the API's rescaled `confidence` field).
The test labels are refused unless --allow-test is given.
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

OUTCOMES = ["APPROVE_BUYER", "DENY", "REQUEST_EVIDENCE", "ESCALATE"]


def load_jsonl(path):
    return [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]


def pct(x):
    return f"{100 * x:5.1f}%"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True)
    ap.add_argument("--labels", default="", help="default: data/labels_<split>.jsonl from the run's meta file")
    ap.add_argument("--allow-test", action="store_true")
    a = ap.parse_args()

    run_path = Path(a.run)
    meta_path = run_path.with_suffix(".meta.json")
    split = json.loads(meta_path.read_text())["split"] if meta_path.exists() else "dev"
    labels_path = a.labels or f"data/labels_{split}.jsonl"
    if "test" in Path(labels_path).name and not a.allow_test:
        print("Refusing to read test labels. Freeze the setup on dev first, then pass --allow-test.")
        return 2

    gold = {r["case_id"]: r for r in load_jsonl(labels_path)}
    preds = load_jsonl(run_path)
    errors = [p for p in preds if "error" in p]
    rows = []
    for p in preds:
        if "error" in p or p["case_id"] not in gold:
            continue
        probs = p["probabilities"]
        top = max(OUTCOMES, key=lambda o: probs.get(o, 0.0))
        g = gold[p["case_id"]]
        rows.append({"id": p["case_id"], "pred": top, "conf": probs[top], "probs": probs,
                     "gold": g["gold_outcome"], "rule": g["gold_rule"], "hard": g["is_hard"],
                     "latency": p.get("latency_ms")})
    n = len(rows)
    if not n:
        print("No scored rows. Check the run file.")
        return 1
    print(f"Run: {run_path.name}   scored {n} cases   ({len(errors)} request errors, "
          f"{len(set(gold) - {r['id'] for r in rows})} labeled cases without a prediction)\n")

    acc = sum(r["pred"] == r["gold"] for r in rows) / n
    print(f"Overall accuracy: {pct(acc)}   (always-guess-majority baseline: "
          f"{pct(max(Counter(r['gold'] for r in rows).values()) / n)})\n")

    # per class
    print("Per class (gold n | precision | recall | F1)")
    f1s = []
    for o in OUTCOMES:
        tp = sum(r["pred"] == o and r["gold"] == o for r in rows)
        pp = sum(r["pred"] == o for r in rows)
        gp = sum(r["gold"] == o for r in rows)
        pr = tp / pp if pp else 0.0
        rc = tp / gp if gp else 0.0
        f1 = 2 * pr * rc / (pr + rc) if pr + rc else 0.0
        f1s.append(f1)
        print(f"  {o:17s} {gp:3d} | {pct(pr)} | {pct(rc)} | {f1:.2f}")
    print(f"  macro-F1: {statistics.mean(f1s):.2f}\n")

    # confusion
    print("Confusion matrix (rows = gold, columns = predicted)")
    print("  " + " " * 17 + "".join(f"{o[:10]:>12s}" for o in OUTCOMES))
    for g in OUTCOMES:
        print(f"  {g:17s}" + "".join(f"{sum(r['gold'] == g and r['pred'] == p for r in rows):12d}" for p in OUTCOMES))
    print()

    # costly errors
    wrong_refund = [r for r in rows if r["pred"] == "APPROVE_BUYER" and r["gold"] != "APPROVE_BUYER"]
    wrong_deny = [r for r in rows if r["pred"] == "DENY" and r["gold"] == "APPROVE_BUYER"]
    missed_esc = [r for r in rows if r["gold"] == "ESCALATE" and r["pred"] in ("APPROVE_BUYER", "DENY")]
    print("Costly errors")
    print(f"  wrong refund (predicted APPROVE_BUYER, gold was not): {len(wrong_refund)}")
    print(f"  wrong deny (predicted DENY, gold was APPROVE_BUYER):  {len(wrong_deny)}")
    print(f"  missed escalation (gold ESCALATE, auto-decided):       {len(missed_esc)}\n")

    # calibration
    brier = statistics.mean(sum((r["probs"].get(o, 0.0) - (o == r["gold"])) ** 2 for o in OUTCOMES) for r in rows)
    bins = defaultdict(list)
    for r in rows:
        bins[min(int(r["conf"] * 10), 9)].append(r)
    ece = 0.0
    print("Calibration (top-probability bins)   n | mean confidence | accuracy")
    for b in sorted(bins):
        rs = bins[b]
        mc = statistics.mean(r["conf"] for r in rs)
        ac = sum(r["pred"] == r["gold"] for r in rs) / len(rs)
        ece += len(rs) / n * abs(mc - ac)
        print(f"  {b / 10:.1f}-{(b + 1) / 10:.1f}  {len(rs):4d} | {pct(mc)} | {pct(ac)}")
    print(f"  ECE: {ece:.3f}   Brier: {brier:.3f}   (uniform guess Brier = 0.750)\n")
    right = [r["conf"] for r in rows if r["pred"] == r["gold"]]
    wrong = [r["conf"] for r in rows if r["pred"] != r["gold"]]
    if right and wrong:
        print(f"Mean confidence when right: {statistics.mean(right):.3f}   when wrong: {statistics.mean(wrong):.3f}\n")

    # threshold analysis
    print("Auto-decide threshold (decide only if top probability >= t)   coverage | accuracy on decided | wrong refunds | missed escalations")
    for t in (0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95):
        d = [r for r in rows if r["conf"] >= t]
        if not d:
            print(f"  t={t:.2f}      0.0% |        - |  - | -")
            continue
        wr = sum(r["pred"] == "APPROVE_BUYER" and r["gold"] != "APPROVE_BUYER" for r in d)
        me = sum(r["gold"] == "ESCALATE" and r["pred"] in ("APPROVE_BUYER", "DENY") for r in d)
        print(f"  t={t:.2f}   {pct(len(d) / n)} | {pct(sum(r['pred'] == r['gold'] for r in d) / len(d))} | {wr:3d} | {me:3d}")
    print()

    # breakdowns
    def table(title, key):
        print(title)
        groups = defaultdict(list)
        for r in rows:
            groups[key(r)].append(r)
        for k in sorted(groups, key=str):
            rs = groups[k]
            print(f"  {str(k):12s} n={len(rs):3d}  accuracy {pct(sum(r['pred'] == r['gold'] for r in rs) / len(rs))}")
        print()

    table("By gold rule", lambda r: r["rule"])
    table("By difficulty (is_hard)", lambda r: "hard" if r["hard"] else "easy")

    lat = [r["latency"] for r in rows if r["latency"] is not None]
    if lat:
        print(f"Latency (server-reported ms): median {statistics.median(lat):.0f}, "
              f"mean {statistics.mean(lat):.0f}, max {max(lat):.0f}")

    summary = {"run": run_path.name, "n": n, "accuracy": acc, "macro_f1": statistics.mean(f1s), "ece": ece,
               "brier": brier, "wrong_refund": len(wrong_refund), "wrong_deny": len(wrong_deny),
               "missed_escalation": len(missed_esc)}
    run_path.with_suffix(".summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
