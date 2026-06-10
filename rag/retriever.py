"""多链路召回 + RRF 融合。

三路召回：①向量语义召回；②BM25 关键词召回；③结构化过滤召回（城市/类别命中的弱先验）。
用 RRF(Reciprocal Rank Fusion) 把各路排名融合——只看名次不看分数量纲，鲁棒且无需调权重。
"""
from __future__ import annotations

from rag.bm25 import BM25Index
from rag.chunk import Chunk, meta_match
from rag.store import NumpyVectorStore


def rrf_fuse(ranked_lists: list[list[str]], k0: int = 60) -> list[str]:
    """RRF：score(id) = Σ 1/(k0 + rank)；返回按融合分降序的 id 列表（去重）。"""
    scores: dict[str, float] = {}
    for lst in ranked_lists:
        for rank, doc_id in enumerate(lst):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k0 + rank + 1)
    return [doc_id for doc_id, _ in sorted(scores.items(), key=lambda x: -x[1])]


class MultiPathRetriever:
    """向量 + BM25 + 结构化 三路召回，RRF 融合成单一候选排序。"""

    def __init__(self, store: NumpyVectorStore, bm25: BM25Index, embedder, chunks: dict[str, Chunk]):
        self.store = store
        self.bm25 = bm25
        self.embedder = embedder
        self.chunks = chunks

    def _allowed_ids(self, where: dict | None) -> set[str] | None:
        if not where:
            return None
        return {cid for cid, ch in self.chunks.items() if meta_match(ch.meta, where)}

    def recall(self, queries: list[str], where: dict | None = None, k_each: int = 20, k0: int = 60) -> list[str]:
        allowed = self._allowed_ids(where)
        ranked_lists: list[list[str]] = []
        for q in queries:
            qv = self.embedder.embed([q])[0]
            # 路1：向量召回（带结构化过滤）
            ranked_lists.append([cid for cid, *_ in self.store.search(qv, k_each, where)])
            # 路2：BM25 关键词召回（限定在结构化命中集合内）
            ranked_lists.append([cid for cid, _ in self.bm25.search(q, k_each, allowed)])
        # 路3：结构化弱先验（给命中城市/类别的块一个基础名次）
        if allowed:
            ranked_lists.append(list(allowed))
        return rrf_fuse(ranked_lists, k0)
