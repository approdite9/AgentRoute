"""RAG 管线 —— 把切片/向量库/BM25/多链路召回/重排/问题重写串起来。

retrieve(query, where) 流程：问题重写 → 多链路召回(向量+BM25+结构化, RRF 融合) → 重排 → top-k。
get_default_pipeline() 加载内置语料并建好索引（进程级单例），供 LangGraph 的 rag_node 复用。
"""
from __future__ import annotations

import functools
import json
from pathlib import Path

from rag.bm25 import BM25Index
from rag.chunk import Chunk, chunk_documents
from rag.embeddings import get_embedder
from rag.rerank import get_reranker, rerank
from rag.retriever import MultiPathRetriever
from rag.rewrite import rewrite_query
from rag.store import NumpyVectorStore

_CORPUS = Path(__file__).parent / "corpus" / "travel_notes.json"


class RagPipeline:
    def __init__(self, embedder=None, reranker=None, rewriter_llm=None):
        self.embedder = embedder or get_embedder()
        self.reranker = reranker if reranker is not None else get_reranker()
        self.rewriter_llm = rewriter_llm
        self.store = NumpyVectorStore()
        self.bm25 = BM25Index()
        self.chunks: dict[str, Chunk] = {}
        self.retriever: MultiPathRetriever | None = None

    def index(self, docs: list[dict], size: int = 220, overlap: int = 50) -> int:
        chunks = chunk_documents(docs, size=size, overlap=overlap)
        if not chunks:
            return 0
        vectors = self.embedder.embed([c.text for c in chunks])
        self.store.add(
            [c.id for c in chunks], vectors, [c.text for c in chunks], [c.meta for c in chunks]
        )
        self.bm25.index([c.id for c in chunks], [c.text for c in chunks])
        for c in chunks:
            self.chunks[c.id] = c
        self.retriever = MultiPathRetriever(self.store, self.bm25, self.embedder, self.chunks)
        return len(chunks)

    def retrieve(
        self, query: str, where: dict | None = None, k: int = 4, k_recall: int = 20,
        *, multipath: bool = True, rerank_on: bool = True,
    ) -> list[Chunk]:
        """检索 top-k。multipath/rerank_on 用于消融评估（量化每个组件的增益）。"""
        if self.retriever is None or not self.chunks:
            return []
        variants = rewrite_query(query, self.rewriter_llm)
        if multipath:
            fused_ids = self.retriever.recall(variants, where=where, k_each=k_recall)
        else:
            # 消融：仅向量单路召回（不接 BM25 / 结构化 / RRF）。
            qv = self.embedder.embed([variants[0]])[0]
            fused_ids = [cid for cid, *_ in self.store.search(qv, k_recall, where)]
        candidates = [self.chunks[i] for i in fused_ids[:k_recall] if i in self.chunks]
        if not rerank_on:
            return candidates[:k]
        return rerank(query, candidates, top_k=k, reranker=self.reranker)


def load_corpus() -> list[dict]:
    if not _CORPUS.exists():
        return []
    with open(_CORPUS, encoding="utf-8") as f:
        return json.load(f)


@functools.lru_cache(maxsize=1)
def get_default_pipeline() -> RagPipeline:
    """进程级单例：加载内置语料并建索引。embedder/reranker 由环境变量决定线上/离线。"""
    pipe = RagPipeline()
    pipe.index(load_corpus())
    return pipe


def format_context(chunks: list[Chunk], max_chars: int = 1200) -> str:
    """把检索到的片段拼成给 synthesis 的"内容证据"，带出处标签以便溯源、防幻觉。"""
    lines, total = [], 0
    for c in chunks:
        tag = f"[{c.meta.get('city','')}·{c.meta.get('category','')}·{c.meta.get('title','')}]"
        piece = f"{tag} {c.text}"
        if total + len(piece) > max_chars:
            break
        lines.append(piece)
        total += len(piece)
    return "\n".join(lines)


def retrieval_confidence(query: str, chunks: list[Chunk]) -> float:
    """检索置信度（零成本自我反思信号）——query 关键词被 top 片段覆盖的最大比例。

    用**覆盖率**(query 与片段的 token 交集 / query token 数)而非 Jaccard：
    中文 bigram 分词下片段 token 远多于 query，Jaccard 会被片段长度稀释到接近 0
    （命中也判低分 → 几乎每次误触发重检、拖慢体验）；覆盖率只问"query 的词有多少
    出现在片段里"，命中即高分、无关才低分，区分度更干净、阈值更好定。

    **不额外调用 LLM / embedding / 网络**，纯本地 O(片段数) 计算。Agentic RAG 的
    "自我反思"据此判断这批检索够不够好：低于阈值才触发一次放宽重检。返回 [0,1]。
    """
    from rag.tokenize import tokenize

    if not chunks:
        return 0.0
    q = set(tokenize(query))
    if not q:
        return 0.0
    best = 0.0
    for c in chunks:
        d = set(tokenize(c.text))
        cov = len(q & d) / len(q)
        if cov > best:
            best = cov
    return best
