"""Retrieval quality on the dev split. Run: python eval_rag.py

policy:      is the rule that fires (rulebook.label_case) in the top-k policy hits?
precedents:  leave-one-out; does a majority vote of the top-k similar past cases
             recover the gold outcome? (a kNN baseline, not a model)
"""
import json
from collections import Counter

import rag_indexes as rag
import rulebook as rb

K = 3


def main():
    mi = rag.build_multi_index()
    with open(rag.ROOT / "data/cases_dev.jsonl", encoding="utf-8") as f:
        cases = [json.loads(l) for l in f]
    gold = {}
    with open(rag.ROOT / "data/labels_dev.jsonl", encoding="utf-8") as f:
        for l in f:
            r = json.loads(l)
            gold[r["case_id"]] = r["gold_outcome"]

    policy_hit = {1: 0, K: 0}
    knn_ok = knn_n = 0
    for c in cases:
        res = rag.retrieve(mi, c, K)
        _, rule = rb.label_case(c)
        ids = [h.doc.doc_id for h in res["policy"]]
        for k in policy_hit:
            policy_hit[k] += rule in ids[:k]
        votes = Counter(h.doc.meta["outcome"] for h in res["precedents"])
        if votes:
            knn_n += 1
            knn_ok += votes.most_common(1)[0][0] == gold[c["case_id"]]

    n = len(cases)
    print(f"cases: {n}")
    print(f"policy rule hit@1: {policy_hit[1] / n:.2f}   hit@{K}: {policy_hit[K] / n:.2f}")
    print(f"precedent {K}-NN majority outcome accuracy (leave-one-out): {knn_ok / knn_n:.2f}"
          f"  (majority-class baseline: {max(Counter(gold.values()).values()) / n:.2f})")


if __name__ == "__main__":
    main()
