"""Tests for retrieval.py and rag_indexes.py."""
import json

import policy
import rag_indexes as rag
import rulebook as rb
from retrieval import BM25Index, Document, MultiIndex, tokenize

MI = rag.build_multi_index()
CASES = [json.loads(l) for l in open(rag.ROOT / "data/cases_dev.jsonl", encoding="utf-8")]


def test_tokenize():
    assert tokenize("returned_to_sender") == ["returned", "sender"]  # "to" is a stopword
    assert "not" in tokenize("was not delivered")
    assert tokenize("Photos") == ["photo"]


def test_bm25_ranks_relevant_first_and_excludes():
    ix = BM25Index("t", [Document("a", "lost parcel refund"), Document("b", "signed delivery proof"),
                         Document("c", "lost lost package")])
    assert ix.search("lost package")[0].doc.doc_id == "c"
    assert [h.doc.doc_id for h in ix.search("lost", exclude_ids=frozenset({"c"}))] == ["a"]
    assert ix.search("zzzz") == []


def test_policy_and_guidance_parse_completely():
    ids = {d.doc_id for d in MI.indexes["policy"].docs}
    assert ids == {"G1", "G2", "G3", "G4", "G5", "G6", *(f"INR-{i}" for i in range(1, 8)),
                   *(f"SNAD-{i}" for i in range(1, 7))}
    kinds = [d.meta["kind"] for d in MI.indexes["guidance"].docs]
    assert kinds.count("outcome") == 4 and kinds.count("hard_case") == 8 and "precedence" in kinds


def test_precedents_exclude_the_query_case_and_hold_no_extra_labels():
    for c in CASES[:20]:
        res = rag.retrieve(MI, c)
        assert c["case_id"] not in {h.doc.doc_id for h in res["precedents"]}
    for d in MI.indexes["precedents"].docs:
        assert set(d.meta) == {"outcome", "rule"}
        assert "scenario" not in d.text and "gold" not in d.text


def test_render_case_uses_only_input_fields():
    c = dict(CASES[0], gold_outcome="DENY", scenario="secret", is_hard=True)
    text = rag.render_case(c)
    assert "secret" not in text and "gold" not in text


def test_context_has_exact_fired_rule():
    for c in CASES[:15]:
        d = policy.decide(c)
        ctx = rag.build_context(MI, c, d)
        if d.rule_id == rb.DEFAULT_RULE:
            assert ctx["fired_rule"]["text"] is None
        else:
            assert ctx["fired_rule"]["text"].startswith(d.rule_id)
        assert all(not t.startswith(d.rule_id + " ") for t in ctx["related_rules"])
        assert len(ctx["similar_cases"]) <= 3


def test_fuse_merges_across_indexes():
    res = rag.retrieve(MI, CASES[0])
    fused = MultiIndex.fuse(res, k=4)
    assert len(fused) == 4 and {f[0] for f in fused} <= {"policy", "guidance", "precedents"}


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"{len(fns)} rag tests passed")
