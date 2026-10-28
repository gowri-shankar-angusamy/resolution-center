"""Dependency-free keyword retrieval: BM25 indexes plus a multi-index wrapper.

No model downloads. Tokenisation splits on non-alphanumerics (so `returned_to_sender`
becomes three tokens), lowercases, drops a small stopword list and strips a
trailing plural "s".
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

STOPWORDS = frozenset(
    "a an the and or of to in on is are was be by for with as at it its this that "
    "if than from has have had so".split()   # "not" and "no" are deliberately kept
)
_SPLIT = re.compile(r"[^a-z0-9]+")


def tokenize(text: str) -> list[str]:
    out = []
    for tok in _SPLIT.split(text.lower()):
        if not tok or tok in STOPWORDS:
            continue
        if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
            tok = tok[:-1]
        out.append(tok)
    return out


@dataclass(frozen=True)
class Document:
    doc_id: str
    text: str
    meta: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Hit:
    index: str
    doc: Document
    score: float
    rank: int


class BM25Index:
    def __init__(self, name: str, docs: list[Document], k1: float = 1.5, b: float = 0.75):
        self.name, self.docs, self.k1, self.b = name, docs, k1, b
        self._tf = [Counter(tokenize(d.text)) for d in docs]
        self._len = [sum(tf.values()) for tf in self._tf]
        self._avg = (sum(self._len) / len(docs)) if docs else 0.0
        df = Counter()
        for tf in self._tf:
            df.update(tf.keys())
        n = len(docs)
        self._idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def search(self, query: str, k: int = 5, exclude_ids: frozenset = frozenset()) -> list[Hit]:
        q = tokenize(query)
        scored = []
        for i, doc in enumerate(self.docs):
            if doc.doc_id in exclude_ids:
                continue
            tf, dl, s = self._tf[i], self._len[i], 0.0
            for t in q:
                f = tf.get(t)
                if f:
                    s += self._idf[t] * f * (self.k1 + 1) / (
                        f + self.k1 * (1 - self.b + self.b * dl / self._avg))
            if s > 0:
                scored.append((s, i))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [Hit(self.name, self.docs[i], s, r) for r, (s, i) in enumerate(scored[:k], 1)]


class MultiIndex:
    """Named indexes searched with a per-index query (the right query for
    rule text is not the right query for past cases)."""

    def __init__(self, indexes: list[BM25Index]):
        self.indexes = {ix.name: ix for ix in indexes}

    def search(self, queries: dict[str, str], k: int | dict[str, int] = 3,
               exclude_ids: frozenset = frozenset()) -> dict[str, list[Hit]]:
        out = {}
        for name, q in queries.items():
            kk = k[name] if isinstance(k, dict) else k
            out[name] = self.indexes[name].search(q, kk, exclude_ids)
        return out

    @staticmethod
    def fuse(results: dict[str, list[Hit]], k: int = 5, rrf_k: int = 60) -> list[tuple[str, Document, float]]:
        """Reciprocal rank fusion across indexes: one ranked list for display."""
        scores: dict[tuple[str, str], float] = {}
        docs: dict[tuple[str, str], Document] = {}
        for name, hits in results.items():
            for h in hits:
                key = (name, h.doc.doc_id)
                scores[key] = scores.get(key, 0.0) + 1.0 / (rrf_k + h.rank)
                docs[key] = h.doc
        ranked = sorted(scores, key=lambda key: -scores[key])[:k]
        return [(key[0], docs[key], scores[key]) for key in ranked]
