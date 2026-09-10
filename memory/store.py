"""长期记忆 Store —— 按 user_id 持久化用户旅行偏好画像。

存储后端：Redis db=6（沿用 cache/client.py 的连接池风格与优雅降级策略）。
Redis 不可用时退化为进程内内存字典（_MEMORY_FALLBACK），保证 CI / 无 Redis 环境可跑。

数据模型（namespace = "user_memory:{user_id}"）：
    {
      "preferences": ["美食", "历史文化"],   # 长期偏好类别（累积去重）
      "hotel_type": "舒适型",
      "transport": ["地铁", "步行"],
      "origin_city": "成都",
      "party_type": "家庭亲子",
      "budget_level": "舒适适中",
      "updated_at": "2026-09-08T...",
      "visit_count": 3,
    }

用法（由 planner 在规划前后可选调用，不侵入 graph）：
    mem = await get_user_memory(user_id)
    state = merge_memory_into_state(state, mem)      # 规划前：空字段用记忆补全
    ...run graph...
    await update_memory_from_state(user_id, state)   # 规划后：回写本次明确偏好
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import redis.asyncio as aioredis
from redis.exceptions import RedisError, ConnectionError as RedisConnectionError
import structlog

from config import settings

logger = structlog.get_logger(__name__)

# 长期记忆专用 db，与其它用途隔离（见 __init__.py 说明）。
_MEMORY_DB = 6
_KEY_PREFIX = "user_memory:"

# 参与长期记忆的偏好字段（其余如 city/date 是每次行程特有，不进画像）。
# list 型字段累积去重；标量型字段以最新非空值覆盖。
LONG_TERM_PREF_FIELDS = (
    "preferences",    # list[str]
    "transport",      # list[str]
    "hotel_type",     # str
    "origin_city",    # str
    "party_type",     # str
    "budget_level",   # str
)
_LIST_FIELDS = {"preferences", "transport"}

# 列表型偏好的容量上限：累积时只保留最近 N 个（FIFO 淘汰最旧），
# 避免用户长期使用后 preferences/transport 无限增长、撑爆单条记忆。
_LIST_MAX_LEN = 20

# Redis 不可用时的进程内兜底（仅当前进程有效，够 CI / 单机降级用）。
_MEMORY_FALLBACK: dict[str, dict] = {}


class MemoryStore:
    """长期记忆的异步 Redis 连接池单例（沿用 cache.CacheClient 的 loop 绑定策略）。"""

    _pool: aioredis.ConnectionPool | None = None
    _loop: asyncio.AbstractEventLoop | None = None

    @classmethod
    async def get(cls) -> aioredis.Redis:
        loop = asyncio.get_running_loop()
        if cls._pool is None or cls._loop is not loop:
            cls._pool = aioredis.ConnectionPool.from_url(
                settings.redis_url,
                db=_MEMORY_DB,
                max_connections=10,
                decode_responses=True,
            )
            cls._loop = loop
        return aioredis.Redis(connection_pool=cls._pool)


def _key(user_id: str) -> str:
    return f"{_KEY_PREFIX}{user_id}"


async def get_user_memory(user_id: str) -> dict:
    """读取用户长期偏好画像；无记录 / Redis 不可用时返回空 dict（降级到内存兜底）。"""
    if not user_id:
        return {}
    try:
        r = await MemoryStore.get()
        val = await r.get(_key(user_id))
        if val:
            return json.loads(val)
        return {}
    except (RedisError, RedisConnectionError, OSError, asyncio.TimeoutError) as exc:
        logger.warning("memory_get_degraded", user_id=user_id, error=str(exc))
        return dict(_MEMORY_FALLBACK.get(user_id, {}))


async def save_user_memory(user_id: str, memory: dict) -> None:
    """写入用户长期偏好画像（无 TTL，长期保留）；Redis 不可用时写内存兜底。"""
    if not user_id:
        return
    payload = json.dumps(memory, ensure_ascii=False)
    try:
        r = await MemoryStore.get()
        await r.set(_key(user_id), payload)
    except (RedisError, RedisConnectionError, OSError, asyncio.TimeoutError) as exc:
        logger.warning("memory_save_degraded", user_id=user_id, error=str(exc))
        _MEMORY_FALLBACK[user_id] = dict(memory)


def merge_memory_into_state(state: dict, memory: dict) -> dict:
    """规划前：用长期记忆补全 state 中**用户未填**的偏好字段（不覆盖用户本次明确输入）。

    - 标量字段：仅当 state 里为空（""/None）时用记忆值填充。
    - list 字段：仅当 state 里为空列表时用记忆值填充。
    返回新 dict（不原地修改入参）。
    """
    if not memory:
        return dict(state)
    merged = dict(state)
    for field in LONG_TERM_PREF_FIELDS:
        mem_val = memory.get(field)
        if not mem_val:
            continue
        cur = merged.get(field)
        if field in _LIST_FIELDS:
            if not cur:  # None 或 []
                merged[field] = list(mem_val)
        else:
            if not cur:  # None 或 ""
                merged[field] = mem_val
    return merged


def _merge_field(field: str, old, new):
    """把本次 state 的字段值并入记忆：list 累积去重（保序），标量取最新非空。"""
    if field in _LIST_FIELDS:
        merged = list(old or [])
        for item in (new or []):
            if item and item not in merged:
                merged.append(item)
        # 容量上限：超长时保留最近 _LIST_MAX_LEN 个（淘汰最旧），防止无限增长。
        if len(merged) > _LIST_MAX_LEN:
            merged = merged[-_LIST_MAX_LEN:]
        return merged
    return new if new else old


async def update_memory_from_state(user_id: str, state: dict) -> dict:
    """规划后：把本次 state 里用户明确提供的偏好回写记忆（累积/更新），返回更新后的画像。

    只回写 LONG_TERM_PREF_FIELDS；空值不覆盖已有记忆。同时递增 visit_count、刷新 updated_at。
    """
    if not user_id:
        return {}
    memory = await get_user_memory(user_id)
    for field in LONG_TERM_PREF_FIELDS:
        new_val = state.get(field)
        if new_val:  # 空值不回写，避免抹掉历史画像
            memory[field] = _merge_field(field, memory.get(field), new_val)
    memory["visit_count"] = int(memory.get("visit_count", 0)) + 1
    memory["updated_at"] = datetime.now(timezone.utc).isoformat()
    await save_user_memory(user_id, memory)
    logger.info("memory_updated", user_id=user_id, visit_count=memory["visit_count"])
    return memory
