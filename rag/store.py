"""向量库 —— NumpyVectorStore（默认，内存 + 余弦），接口可换 pgvector/Milvus。

向量在 embedding 阶段已 L2 归一，故相似度 = 归一向量点积（余弦）。结构化过滤(where)
在检索时按 chunk 元数据筛选，实现"向量 + 结构化"的联合召回。
"""
from __future__ import annotations

import numpy as np

from rag.chunk import meta_match


class NumpyVectorStore:
    def __init__(self) -> None:
        self._ids: list[str] = []
        self._texts: list[str] = []
        self._meta: list[dict] = []
        self._mat: np.ndarray | None = None

    def __len__(self) -> int:
        return len(self._ids)

    def add(self, ids, vectors, texts, metadatas) -> None:
        arr = np.asarray(vectors, dtype=np.float32)
        self._mat = arr if self._mat is None else np.vstack([self._mat, arr])
        self._ids.extend(ids)
        self._texts.extend(texts)
        self._meta.extend(metadatas)

    def search(self, query_vec, k: int = 20, where: dict | None = None) -> list[tuple[str, float, str, dict]]:
        if self._mat is None or len(self._ids) == 0:
            return []
        q = np.asarray(query_vec, dtype=np.float32)
        sims = self._mat @ q  # 归一向量点积 = 余弦
        order = np.argsort(-sims)
        out: list[tuple[str, float, str, dict]] = []
        for i in order:
            if where and not meta_match(self._meta[i], where):
                continue
            out.append((self._ids[i], float(sims[i]), self._texts[i], self._meta[i]))
            if len(out) >= k:
                break
        return out
