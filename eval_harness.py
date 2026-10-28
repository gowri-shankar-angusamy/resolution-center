"""Evaluation harness: score any system's decisions against gold labels.

  python eval_harness.py --system rulebook                 # on the frozen gold set (default)
  python eval_harness.py --system knn                      # 3-NN over dev precedents
  python eval_harness.py --predictions runs/kev-....jsonl  # a saved Kev run (use --labels dev)
  python eval_harness.py --system rulebook --labels dev

Label sets: `gold` (data/gold_labels.jsonl, hand-labeled) or `dev`. The test split is not
available here on purpose. Predictors only ever see case inputs; labels are joined on
case_id at scoring time. A predictions file is JSONL with case_id and either `outcome` or
`choice`, plus optional `confidence`, `probabilities` and `rule_id`.

Metric definitions match score_run.py (wrong refund, wrong deny, missed escalation) and add
ESCALATE as a first-class output: precision, recall, over-escalation and the share of cases
routed to a human. With small n, read the Wilson intervals, not the point estimates.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable, Optional

import rulebook as rb

ROOT = Path(__file__).parent
OUTCOMES = rb.OUTCOMES


def _jsonl(path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ---------------- label sets ----------------
def load_labelset(name: str) -> tuple[list[dict], dict[str, dict], Callable[[dict], dict]]:
    """(cases, labels by case_id, group function). `test` is deliberately unsupported."""
    if name == "gold":
        cases = _jsonl(ROOT / "data/gold_cases.jsonl")
        labels = {r["case_id"]: r for r in _jsonl(ROOT / "data/gold_labels.jsonl")}
        pattern = {m["case_id"]: m["pattern"] for m in _jsonl(ROOT / "data/gold_meta.jsonl")}

        def groups(row):
            return {"pattern": pattern.get(row["case_id"], "?"),
                    "human confidence": f"conf {row['human_confidence']}",
                    "rulebook": "agrees" if row["agrees_with_rulebook"] else f"gap:{row['resolution']}"}
        return cases, labels, groups
    if name == "dev":
        cases = _jsonl(ROOT / "data/cases_dev.jsonl")
        labels = {r["case_id"]: r for r in _jsonl(ROOT / "data/labels_dev.jsonl")}
        return cases, labels, lambda row: {"gold rule": row["gold_rule"],
                                           "difficulty": "hard" if row["is_hard"] else "easy"}
    raise SystemExit(f"unknown label set {name!r}; choose gold or dev (test is not available here)")


# ---------------- systems under test ----------------
def predict_rulebook(cases: list[dict]) -> list[dict]:
    out = []
    for c in cases:
        o, r = rb.label_case(c)
        out.append({"case_id": c["case_id"], "outcome": o, "rule_id": r, "confidence": 1.0})
    return out


def predict_knn(cases: list[dict], k: int = 3) -> list[dict]:
    """Majority vote of the k most similar dev precedents. A retrieval baseline, not a model."""
    import rag_indexes as rag
    mi = rag.build_multi_index()
    out = []
    for c in cases:
        hits = mi.indexes["precedents"].search(rag.render_case(c), k, frozenset({c["case_id"]}))
        votes = Counter(h.doc.meta["outcome"] for h in hits)
        if votes:
            top, n = votes.most_common(1)[0]
            out.append({"case_id": c["case_id"], "outcome": top, "confidence": n / len(hits)})
    return out


def load_predictions(path) -> list[dict]:
    out = []
    for r in _jsonl(path):
        if "error" in r:
            continue
        probs = r.get("probabilities")
        outcome = r.get("outcome") or (max(OUTCOMES, key=lambda o: probs.get(o, 0.0)) if probs else r.get("choice"))
        conf = r.get("confidence")
        if conf is None and probs and outcome in probs:
            conf = probs[outcome]
        out.append({"case_id": r["case_id"], "outcome": outcome, "confidence": conf,
                    "rule_id": r.get("rule_id"), "probabilities": probs})
    return out


# ---------------- scoring ----------------
def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def _prf(rows, o):
    tp = sum(r["pred"] == o and r["gold"] == o for r in rows)
    pp = sum(r["pred"] == o for r in rows)
    gp = sum(r["gold"] == o for r in rows)
    pr, rc = (tp / pp if pp else 0.0), (tp / gp if gp else 0.0)
    return {"gold_n": gp, "predicted_n": pp, "precision": pr, "recall": rc,
            "f1": 2 * pr * rc / (pr + rc) if pr + rc else 0.0}


def score(preds: list[dict], labels: dict[str, dict], groups: Callable[[dict], dict]) -> dict:
    by_id = {p["case_id"]: p for p in preds}
    rows, missing = [], []
    for cid, lab in labels.items():
        p = by_id.get(cid)
        if p is None or p.get("outcome") not in OUTCOMES:
            missing.append(cid)
            continue
        rows.append({"id": cid, "pred": p["outcome"], "gold": lab["gold_outcome"], "conf": p.get("confidence"),
                     "probs": p.get("probabilities"), "pred_rule": p.get("rule_id"), "gold_rule": lab["gold_rule"],
                     "groups": groups(lab)})
    n = len(rows)
    res: dict = {"n": n, "missing": missing}
    if not n:
        return res
    ok = sum(r["pred"] == r["gold"] for r in rows)
    res["accuracy"] = ok / n
    res["accuracy_ci95"] = wilson(ok, n)
    res["majority_baseline"] = max(Counter(r["gold"] for r in rows).values()) / n
    res["per_class"] = {o: _prf(rows, o) for o in OUTCOMES}
    res["macro_f1"] = statistics.mean(v["f1"] for v in res["per_class"].values())
    res["confusion"] = {g: {p: sum(r["gold"] == g and r["pred"] == p for r in rows) for p in OUTCOMES} for g in OUTCOMES}

    res["wrong_refund"] = sum(r["pred"] == rb.APPROVE and r["gold"] != rb.APPROVE for r in rows)
    res["wrong_deny"] = sum(r["pred"] == rb.DENY and r["gold"] == rb.APPROVE for r in rows)
    res["missed_escalation"] = sum(r["gold"] == rb.ESCALATE and r["pred"] in (rb.APPROVE, rb.DENY) for r in rows)
    esc = res["per_class"][rb.ESCALATE]
    res["escalation"] = {
        "precision": esc["precision"], "recall": esc["recall"],
        "over_escalated": sum(r["pred"] == rb.ESCALATE and r["gold"] != rb.ESCALATE for r in rows),
        "routed_to_human_rate": sum(r["pred"] in (rb.ESCALATE, rb.REQUEST) for r in rows) / n,
        "auto_decided_rate": sum(r["pred"] in (rb.APPROVE, rb.DENY) for r in rows) / n,
    }
    scored_rules = [r for r in rows if r["pred_rule"]]
    if scored_rules:
        res["rule_citation_accuracy"] = sum(r["pred_rule"] == r["gold_rule"] for r in scored_rules) / len(scored_rules)

    confs = [r for r in rows if r["conf"] is not None]
    if confs:
        bins = defaultdict(list)
        for r in confs:
            bins[min(int(r["conf"] * 10), 9)].append(r)
        res["ece"] = sum(len(v) / len(confs) * abs(statistics.mean(x["conf"] for x in v)
                                                   - sum(x["pred"] == x["gold"] for x in v) / len(v))
                         for v in bins.values())
    breakdown: dict = defaultdict(dict)
    for r in rows:
        for dim, val in r["groups"].items():
            breakdown[dim].setdefault(val, []).append(r)
    res["breakdown"] = {dim: {val: {"n": len(rs), "accuracy": sum(x["pred"] == x["gold"] for x in rs) / len(rs)}
                              for val, rs in sorted(vals.items())} for dim, vals in breakdown.items()}
    res["errors"] = [{"case_id": r["id"], "gold": r["gold"], "pred": r["pred"]} for r in rows if r["pred"] != r["gold"]]
    return res


# ---------------- report ----------------
def format_report(name: str, labelset: str, res: dict) -> str:
    if not res["n"]:
        return f"{name} on {labelset}: no scored cases ({len(res['missing'])} missing)"
    pct = lambda x: f"{100 * x:5.1f}%"
    lo, hi = res["accuracy_ci95"]
    L = [f"System: {name}   labels: {labelset}   scored {res['n']} cases   ({len(res['missing'])} without a prediction)", "",
         f"Accuracy {pct(res['accuracy'])}  (95% CI {pct(lo)} to {pct(hi)};  majority-class baseline {pct(res['majority_baseline'])})",
         f"Macro-F1 {res['macro_f1']:.2f}"]
    if res["n"] < 30:
        L.append(f"NOTE: n={res['n']} is small; differences of a few cases are noise.")
    L += ["", "Per class (gold n | predicted n | precision | recall | F1)"]
    for o, v in res["per_class"].items():
        L.append(f"  {o:17s} {v['gold_n']:3d} | {v['predicted_n']:3d} | {pct(v['precision'])} | {pct(v['recall'])} | {v['f1']:.2f}")
    L += ["", "Confusion (rows gold, columns predicted)", "  " + " " * 17 + "".join(f"{o[:10]:>12s}" for o in OUTCOMES)]
    for g, row in res["confusion"].items():
        L.append(f"  {g:17s}" + "".join(f"{row[p]:12d}" for p in OUTCOMES))
    e = res["escalation"]
    L += ["", "Costly errors",
          f"  wrong refund (predicted APPROVE_BUYER, gold was not): {res['wrong_refund']}",
          f"  wrong deny (predicted DENY, gold was APPROVE_BUYER):  {res['wrong_deny']}",
          f"  missed escalation (gold ESCALATE, auto-decided):       {res['missed_escalation']}", "",
          "Escalation as a first-class output",
          f"  precision {pct(e['precision'])} | recall {pct(e['recall'])} | over-escalated cases {e['over_escalated']}",
          f"  routed to a human (ESCALATE or REQUEST_EVIDENCE) {pct(e['routed_to_human_rate'])} | auto-decided {pct(e['auto_decided_rate'])}"]
    if "rule_citation_accuracy" in res:
        L.append(f"\nRule citation accuracy {pct(res['rule_citation_accuracy'])}")
    if "ece" in res:
        L.append(f"Calibration ECE {res['ece']:.3f}")
    for dim, vals in res["breakdown"].items():
        L += ["", f"By {dim}"] + [f"  {k:22s} n={v['n']:3d}  accuracy {pct(v['accuracy'])}" for k, v in vals.items()]
    if res["errors"]:
        L += ["", "Errors (gold -> predicted)"] + [f"  {x['case_id']}: {x['gold']} -> {x['pred']}" for x in res["errors"]]
    return "\n".join(L)


def run(system: Optional[str], predictions: Optional[str], labelset: str) -> tuple[str, dict]:
    cases, labels, groups = load_labelset(labelset)
    if predictions:
        name, preds = Path(predictions).stem, load_predictions(predictions)
    elif system == "rulebook":
        name, preds = "rulebook", predict_rulebook(cases)
    elif system == "knn":
        name, preds = "knn-3", predict_knn(cases)
    else:
        raise SystemExit("give --system rulebook|knn or --predictions FILE")
    return name, score(preds, labels, groups)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--system", choices=["rulebook", "knn"])
    ap.add_argument("--predictions")
    ap.add_argument("--labels", default="gold", help="gold (default) or dev")
    ap.add_argument("--no-save", action="store_true")
    a = ap.parse_args(argv)
    name, res = run(a.system, a.predictions, a.labels)
    print(format_report(name, a.labels, res))
    if res["n"] and not a.no_save:
        out = ROOT / "runs" / f"{name}_{a.labels}.eval.json"
        out.write_text(json.dumps(res, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
