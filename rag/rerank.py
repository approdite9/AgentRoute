"""重排(rerank) —— 召回阶段拉宽召回率，重排阶段提升精确率。

默认离线兜底：词面重叠(Jaccard) 精排（确定性、可测）。配 RAG_RERANKER=dashscope 切到
DashScope gte-rerank（cross-encoder 级别，质量更高）。两者签名一致，可热插拔。
"""
from __future__ import annotations

import os

from rag.chunk import Chunk
from rag.tokenize import tokenize


def _lexical_overlap(query: str, text: str) -> float:
    q, d = set(tokenize(query)), set(tokenize(text))
    if not q or not d:
        return 0.0
    return len(q & d) / len(q | d)  # Jaccard


def lexical_rerank(query: str, chunks: list[Chunk], top_k: int) -> list[Chunk]:
    scored = sorted(chunks, key=lambda c: -_lexical_overlap(query, c.text))
    return scored[:top_k]


class DashScopeReranker:
    model = "gte-rerank"

    def __call__(self, query: str, chunks: list[Chunk], top_k: int) -> list[Chunk]:
        import dashscope

        from config import settings

        resp = dashscope.TextReRank.call(
            model=self.model,
            query=query,
            documents=[c.text for c in chunks],
            top_n=top_k,
            return_documents=False,
            api_key=settings.dashscope_api_key,
        )
        results = resp.output["results"]  # [{index, relevance_score}]
        return [chunks[r["index"]] for r in results]


def get_reranker():
    """默认 None（用词面重叠兜底）；RAG_RERANKER=dashscope 才走线上 cross-encoder。"""
    if os.getenv("RAG_RERANKER") == "dashscope":
        return DashScopeReranker()
    return None


def rerank(query: str, chunks: list[Chunk], top_k: int, reranker=None) -> list[Chunk]:
    if not chunks:
        return []
    if reranker is not None:
        try:
            return reranker(query, chunks, top_k)
        except Exception:  # noqa: BLE001 —— 线上 rerank 失败回退词面重排，不阻断检索
            pass
    return lexical_rerank(query, chunks, top_k)
