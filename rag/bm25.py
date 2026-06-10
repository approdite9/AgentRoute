"""BM25 关键词召回 —— 自实现 Okapi BM25（不依赖 rank_bm25），与向量召回互补。

向量召回擅长语义相近但措辞不同，BM25 擅长精确关键词/专名（景点名、菜名）；两路融合
能同时治"语义漂移"和"关键词漏召"。
"""
from __future__ import annotations

import math
from collections import Counter

from rag.tokenize import tokenize


class BM25Index:
    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self._ids: list[str] = []
        self._docs: list[list[str]] = []
        self._tf: list[Counter] = []
        self._df: Counter = Counter()
        self._avgdl = 0.0

    def index(self, ids: list[str], texts: list[str]) -> None:
        self._ids = list(ids)
        self._docs = [tokenize(t) for t in texts]
        self._tf = [Counter(d) for d in self._docs]
        self._df = Counter()
        for tf in self._tf:
            self._df.update(tf.keys())
        self._avgdl = (sum(len(d) for d in self._docs) / len(self._docs)) if self._docs else 0.0

    def _idf(self, term: str) -> float:
        n = len(self._docs)
        df = self._df.get(term, 0)
        # BM25 标准 idf（加 0.5 平滑，clamp 到非负）。
        return max(0.0, math.log((n - df + 0.5) / (df + 0.5) + 1.0))

    def search(self, query: str, k: int = 20, allowed_ids: set[str] | None = None) -> list[tuple[str, float]]:
        q_terms = tokenize(query)
        scored: list[tuple[str, float]] = []
        for i, doc_id in enumerate(self._ids):
            if allowed_ids is not None and doc_id not in allowed_ids:
                continue
            tf = self._tf[i]
            dl = len(self._docs[i]) or 1
            score = 0.0
            for term in q_terms:
                f = tf.get(term, 0)
                if not f:
                    continue
                idf = self._idf(term)
                denom = f + self.k1 * (1 - self.b + self.b * dl / (self._avgdl or 1.0))
                score += idf * (f * (self.k1 + 1)) / denom
            if score > 0:
                scored.append((doc_id, score))
        scored.sort(key=lambda x: -x[1])
        return scored[:k]
