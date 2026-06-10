"""向量化 —— 默认确定性离线 HashingEmbedder（免配额、可测），可切 DashScope text-embedding-v3。

切换：环境变量 RAG_EMBEDDER=dashscope（且有 DASHSCOPE_API_KEY）→ 走线上 embedding；
否则用 HashingEmbedder（把 token 哈希进固定维度 + L2 归一，相似文本向量相近，足够做检索与测试）。
两者都返回 **L2 归一** 向量，故向量库用点积即等于余弦相似度。
"""
from __future__ import annotations

import hashlib
import math
import os
from typing import Protocol


class Embedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def _l2norm(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / n for v in vec]


class HashingEmbedder:
    """确定性哈希向量化：token → 桶，词频加权，L2 归一。无需联网、可复现。"""

    def __init__(self, dim: int = 256):
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        from rag.tokenize import tokenize

        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dim
            for tok in tokenize(text):
                h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
                vec[h % self.dim] += 1.0
            out.append(_l2norm(vec))
        return out


class DashScopeEmbedder:
    """DashScope text-embedding-v3（1024 维）。按 batch≤10 分批调用，结果 L2 归一。"""

    dim = 1024
    model = "text-embedding-v3"

    def embed(self, texts: list[str]) -> list[list[float]]:
        import dashscope

        from config import settings

        out: list[list[float]] = []
        for i in range(0, len(texts), 10):
            batch = texts[i : i + 10]
            resp = dashscope.TextEmbedding.call(
                model=self.model, input=batch, api_key=settings.dashscope_api_key
            )
            embs = sorted(resp.output["embeddings"], key=lambda e: e["text_index"])
            out.extend(_l2norm(e["embedding"]) for e in embs)
        return out


def get_embedder() -> Embedder:
    """默认 Hashing（离线/免费/确定性）；显式 RAG_EMBEDDER=dashscope 才走线上。"""
    if os.getenv("RAG_EMBEDDER") == "dashscope":
        try:
            from config import settings

            if settings.dashscope_api_key:
                return DashScopeEmbedder()
        except Exception:  # noqa: BLE001 —— 取不到 key 就回退
            pass
    return HashingEmbedder()
