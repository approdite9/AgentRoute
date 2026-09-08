"""A5 测试 —— 语义缓存 + OpenTelemetry no-op 降级。

- 语义缓存：余弦相似度纯函数 + 降级往返（无 Redis 时 get 视为 miss、set 不抛）。
- OTel：无 opentelemetry 依赖 / 未配 endpoint 时 init 返回 False、span 为 no-op 且不吞异常。
"""
from __future__ import annotations

import pytest

from cache.semantic import _cosine, _embed_one, semantic_get, semantic_set
from monitoring import otel

pytestmark = pytest.mark.anyio


# ---------- 余弦相似度纯函数 ----------

def test_cosine_identical_normalized():
    # 同一向量归一后余弦为 1
    from rag.embeddings import get_embedder
    v = get_embedder().embed(["成都三日游"])[0]
    assert _cosine(v, v) == pytest.approx(1.0, abs=1e-6)


def test_cosine_empty_is_zero():
    assert _cosine([], [1.0]) == 0.0
    assert _cosine([1.0], []) == 0.0


def test_cosine_similar_queries_high():
    """语义相近的 query 相似度应明显高于不相关 query。"""
    from rag.embeddings import get_embedder
    emb = get_embedder()
    a = emb.embed(["成都美食推荐"])[0]
    b = emb.embed(["成都好吃的推荐"])[0]
    c = emb.embed(["北京故宫门票"])[0]
    assert _cosine(a, b) > _cosine(a, c)


def test_embed_one_returns_vector():
    v = _embed_one("成都三日游")
    assert isinstance(v, list) and len(v) > 0


# ---------- 语义缓存降级往返（无 Redis 时 get=miss、set 不抛）----------

async def test_semantic_get_empty_query_is_none():
    assert await semantic_get("") is None


async def test_semantic_set_and_get_no_redis_graceful():
    # 无 Redis 环境：set 静默跳过、get 返回 None，均不抛异常
    await semantic_set("成都三日游", {"plan": "x"}, namespace="test")
    result = await semantic_get("成都玩三天", namespace="test")
    assert result is None or isinstance(result, dict)


# ---------- OTel no-op 降级 ----------

def test_otel_disabled_without_endpoint(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    # 重置初始化状态以便重新判定
    otel._INITIALIZED = False
    otel._TRACER = None
    assert otel.init_tracing("test-svc") is False
    assert otel.is_tracing_enabled() is False


def test_otel_span_noop_yields_none(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    otel._INITIALIZED = False
    otel._TRACER = None
    otel.init_tracing("test-svc")
    with otel.span("some_op", city="成都") as sp:
        assert sp is None  # no-op span


def test_otel_span_noop_does_not_swallow_exceptions(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    otel._INITIALIZED = False
    otel._TRACER = None
    otel.init_tracing("test-svc")
    with pytest.raises(ValueError):
        with otel.span("boom"):
            raise ValueError("should propagate")


def test_otel_init_idempotent(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    otel._INITIALIZED = False
    otel._TRACER = None
    first = otel.init_tracing("svc")
    second = otel.init_tracing("svc")
    assert first == second
