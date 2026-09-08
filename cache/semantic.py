"""语义缓存（A5）—— 相似 query 命中缓存，降低重复 LLM/检索开销。

与 cache/client.py 的**精确匹配**缓存互补：精确 miss 后，用向量相似度找「语义等价」的
历史 query，命中则复用其答案。典型场景：「成都三日游」vs「成都玩三天」应命中同一份结果。

设计（沿用项目风格）：
- 复用 rag.embeddings.get_embedder()：默认 HashingEmbedder（离线/确定性/L2 归一，点积=余弦），
  免配额、可测；RAG_EMBEDDER=dashscope 时自动走线上 embedding。
- 存储：Redis **db=7**（与 cache 精确匹配的 db2 分开，便于独立清理/统计）。
  每条记录存 {vector, answer}；查询时线性扫描候选集算余弦相似度取最优。
- 优雅降级：Redis / embedder 不可用时，get 视为 miss、set 静默跳过，不阻断业务。
- 相似度阈值默认 0.92（HashingEmbedder 下较严，避免误命中）；可按需调。
"""
from __future__ import annotations

import asyncio
import json

import redis.asyncio as aioredis
from redis.exceptions import RedisError, ConnectionError as RedisConnectionError
import structlog

from config import settings

logger = structlog.get_logger(__name__)

_SEMANTIC_DB = 7
_KEY_PREFIX = "semcache:"          # semcache:{namespace}:{idx}
_INDEX_KEY = "semcache_index:"     # semcache_index:{namespace} -> Redis SET of member keys
_DEFAULT_THRESHOLD = 0.92
_MAX_CANDIDATES = 200               # 单 namespace 线性扫描上限，防止无限增长拖慢查询


class SemanticCacheClient:
    """语义缓存的异步 Redis 连接池单例（沿用 cache.CacheClient 的 loop 绑定策略）。"""

    _pool: aioredis.ConnectionPool | None = None
    _loop: asyncio.AbstractEventLoop | None = None

    @classmethod
    async def get(cls) -> aioredis.Redis:
        loop = asyncio.get_running_loop()
        if cls._pool is None or cls._loop is not loop:
            cls._pool = aioredis.ConnectionPool.from_url(
                settings.redis_url,
                db=_SEMANTIC_DB,
                max_connections=10,
                decode_responses=True,
            )
            cls._loop = loop
        return aioredis.Redis(connection_pool=cls._pool)


def _cosine(a: list[float], b: list[float]) -> float:
    """两个 L2 归一化向量的余弦相似度（= 点积）。长度不等时取较短长度。"""
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    return sum(a[i] * b[i] for i in range(n))


def _embed_one(text: str) -> list[float] | None:
    """把 query 向量化；embedder 不可用时返回 None（视为无法语义缓存）。"""
    try:
        from rag.embeddings import get_embedder
        return get_embedder().embed([text])[0]
    except Exception as exc:  # noqa: BLE001 —— embedder 不可用不阻断业务
        logger.warning("semantic_embed_failed", error=str(exc))
        return None


async def semantic_get(
    query: str, namespace: str = "default", threshold: float = _DEFAULT_THRESHOLD
) -> dict | None:
    """查语义缓存：返回相似度 ≥ threshold 的最优历史答案，无命中 / 降级时返回 None。"""
    if not query:
        return None
    qvec = _embed_one(query)
    if qvec is None:
        return None
    try:
        r = await SemanticCacheClient.get()
        members = await r.smembers(f"{_INDEX_KEY}{namespace}")
        if not members:
            return None
        best_sim, best_answer = 0.0, None
        for key in list(members)[:_MAX_CANDIDATES]:
            raw = await r.get(key)
            if not raw:
                continue
            rec = json.loads(raw)
            sim = _cosine(qvec, rec.get("vector", []))
            if sim > best_sim:
                best_sim, best_answer = sim, rec.get("answer")
        if best_answer is not None and best_sim >= threshold:
            logger.info("semantic_cache_hit", namespace=namespace, sim=round(best_sim, 4))
            return {"answer": best_answer, "similarity": round(best_sim, 4)}
        return None
    except (RedisError, RedisConnectionError, OSError, asyncio.TimeoutError) as exc:
        logger.warning("semantic_get_degraded", error=str(exc))
        return None


async def semantic_set(
    query: str, answer: dict, namespace: str = "default", ttl: int = 24 * 3600
) -> None:
    """写语义缓存：向量化 query 并连同答案存入；Redis/embedder 不可用时静默跳过。"""
    if not query:
        return
    qvec = _embed_one(query)
    if qvec is None:
        return
    try:
        r = await SemanticCacheClient.get()
        # 用 query 的稳定哈希做成员 key，相同 query 覆盖而非重复堆积。
        import hashlib
        qid = hashlib.md5(query.encode("utf-8")).hexdigest()[:16]
        member_key = f"{_KEY_PREFIX}{namespace}:{qid}"
        payload = json.dumps({"vector": qvec, "answer": answer, "query": query},
                             ensure_ascii=False)
        await r.setex(member_key, ttl, payload)
        await r.sadd(f"{_INDEX_KEY}{namespace}", member_key)
    except (RedisError, RedisConnectionError, OSError, asyncio.TimeoutError) as exc:
        logger.warning("semantic_set_degraded", error=str(exc))
