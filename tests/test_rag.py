"""RAG 管线测试 —— 全 hermetic（HashingEmbedder + 词面重排，无需联网/配额）。

覆盖：分词 · 切片(重叠/元数据) · 结构化过滤 · 向量库 · BM25 · RRF 融合 · 多链路召回 ·
重排(含失败回退) · 问题重写(含离线兜底) · 端到端检索 · RAG 评估指标。
"""
import pytest

from rag.tokenize import tokenize
from rag.chunk import Chunk, chunk_text, chunk_documents, meta_match
from rag.embeddings import HashingEmbedder
from rag.bm25 import BM25Index
from rag.store import NumpyVectorStore
from rag.retriever import rrf_fuse, MultiPathRetriever
from rag.rerank import rerank, lexical_rerank
from rag.rewrite import rewrite_query
from rag.pipeline import RagPipeline, get_default_pipeline, format_context
from rag import eval as rag_eval


# ==================== 分词 / 切片 ====================

def test_tokenize_mixed():
    toks = tokenize("成都Hotpot火锅")
    assert "hotpot" in toks            # 英文整词
    assert "火锅" in toks               # 中文 bigram
    assert "成都" in toks


def test_chunk_overlap_and_meta():
    docs = [{"doc_id": "d1", "city": "成都", "category": "美食", "title": "t",
             "text": "第一句话很长。" * 30}]
    chunks = chunk_documents(docs, size=80, overlap=20)
    assert len(chunks) >= 2                       # 长文本被切多块
    assert all(c.meta["city"] == "成都" for c in chunks)
    assert chunks[0].id == "d1#0" and chunks[1].id == "d1#1"


def test_meta_match():
    m = {"city": "成都", "category": "美食"}
    assert meta_match(m, {"city": "成都"})
    assert meta_match(m, {"category": ["美食", "玩法"]})   # 列表=任一命中
    assert not meta_match(m, {"city": "长沙"})
    assert meta_match(m, None)                            # 无过滤=全通过


# ==================== 向量化 / 向量库 ====================

def test_hashing_embedder_deterministic_normalized():
    e = HashingEmbedder(dim=64)
    v1 = e.embed(["成都火锅"])[0]
    v2 = e.embed(["成都火锅"])[0]
    assert v1 == v2                                       # 确定性
    assert abs(sum(x * x for x in v1) - 1.0) < 1e-6       # L2 归一
    # 相似文本余弦 > 不相关文本
    import numpy as np
    a, b, c = (np.array(x) for x in e.embed(["成都火锅串串", "成都火锅麻辣", "北京故宫长城"]))
    assert float(a @ b) > float(a @ c)


def test_vector_store_search_and_filter():
    e = HashingEmbedder(dim=64)
    texts = ["成都火锅很辣", "长沙夜宵小龙虾", "成都熊猫基地"]
    metas = [{"city": "成都"}, {"city": "长沙"}, {"city": "成都"}]
    store = NumpyVectorStore()
    store.add(["a", "b", "c"], e.embed(texts), texts, metas)
    qv = e.embed(["成都"])[0]
    # 结构化过滤只返回成都
    hits = store.search(qv, k=5, where={"city": "成都"})
    assert {h[0] for h in hits} == {"a", "c"}
    assert len(store) == 3


# ==================== BM25 / RRF / 多链路 ====================

def test_bm25_keyword_recall():
    idx = BM25Index()
    idx.index(["a", "b"], ["成都火锅毛肚鸭肠", "北京烤鸭故宫"])
    hits = idx.search("火锅", k=5)
    assert hits and hits[0][0] == "a"                     # 关键词命中正确文档


def test_rrf_fuse_rewards_agreement():
    # 两路都把 x 排前 → x 融合分最高
    fused = rrf_fuse([["x", "y", "z"], ["x", "z", "y"]])
    assert fused[0] == "x"


def test_multipath_retriever_combines_paths():
    e = HashingEmbedder(dim=128)
    chunks = {
        "a": Chunk("a", "成都火锅毛肚", {"city": "成都", "category": "美食"}),
        "b": Chunk("b", "成都大熊猫基地", {"city": "成都", "category": "玩法"}),
        "c": Chunk("c", "长沙小龙虾夜宵", {"city": "长沙", "category": "美食"}),
    }
    store = NumpyVectorStore()
    store.add(list(chunks), e.embed([c.text for c in chunks.values()]),
              [c.text for c in chunks.values()], [c.meta for c in chunks.values()])
    bm = BM25Index(); bm.index(list(chunks), [c.text for c in chunks.values()])
    r = MultiPathRetriever(store, bm, e, chunks)
    fused = r.recall(["成都火锅"], where={"city": "成都"})
    assert set(fused) <= {"a", "b"}                       # 结构化过滤掉了长沙
    assert fused[0] == "a"                                 # 火锅关键词把美食块顶到第一


# ==================== 重排 ====================

def test_lexical_rerank_orders_by_overlap():
    chunks = [Chunk("1", "北京故宫"), Chunk("2", "成都火锅毛肚鸭肠")]
    out = lexical_rerank("火锅毛肚", chunks, top_k=2)
    assert out[0].id == "2"


def test_rerank_falls_back_when_reranker_raises():
    def broken(query, chunks, top_k):
        raise RuntimeError("rerank service down")
    chunks = [Chunk("1", "成都火锅"), Chunk("2", "北京烤鸭")]
    out = rerank("火锅", chunks, top_k=1, reranker=broken)   # 不应抛错
    assert len(out) == 1 and out[0].id == "1"               # 回退到词面重排


# ==================== 问题重写 ====================

def test_rewrite_offline_returns_original():
    assert rewrite_query("成都三日游", llm=None) == ["成都三日游"]
    assert rewrite_query("", llm=None) == []


def test_rewrite_with_fake_llm_dedups():
    class _Resp:
        content = "成都美食推荐\n成都必吃小吃\n成都三日游"
    class _LLM:
        def invoke(self, messages):
            return _Resp()
    out = rewrite_query("成都三日游", llm=_LLM(), n=3)
    assert out[0] == "成都三日游"                            # 原查询在首
    assert "成都美食推荐" in out
    assert out.count("成都三日游") == 1                      # 去重


# ==================== 端到端 / 评估 ====================

def test_pipeline_end_to_end():
    pipe = RagPipeline(embedder=HashingEmbedder(dim=128))
    n = pipe.index([
        {"doc_id": "cd", "city": "成都", "category": "美食", "title": "成都美食", "text": "成都火锅毛肚鸭肠串串香钵钵鸡。"},
        {"doc_id": "bj", "city": "北京", "category": "历史文化", "title": "北京", "text": "故宫长城颐和园天坛。"},
    ])
    assert n >= 2
    hits = pipe.retrieve("成都火锅", where={"city": "成都"}, k=2)
    assert hits and hits[0].meta["city"] == "成都"
    ctx = format_context(hits)
    assert "成都" in ctx and "[" in ctx                      # 带出处标签


def test_default_pipeline_eval_recall():
    pipe = get_default_pipeline()
    report = rag_eval.evaluate(pipe, k=4)
    assert report.n == len(rag_eval.EVAL_SET)
    assert report.recall >= 0.9                             # 内置语料召回应很高
    assert 0.0 <= report.precision <= 1.0
    assert report.mrr > 0.5


def test_faithfulness_metric():
    ctx = ["成都火锅以牛油锅底地道，毛肚鸭肠必点。"]
    assert rag_eval.faithfulness("成都火锅牛油锅底很地道。", ctx) >= 0.5   # 被上下文支撑
    assert rag_eval.faithfulness("北京烤鸭天坛公园。", ctx) < 0.5          # 无支撑→低分
