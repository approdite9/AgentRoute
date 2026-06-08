"""
Redis 缓存层客户端 —— 异步连接池 + 通用读写 + 节点函数缓存装饰器。

设计要点：
  - 连接使用独立的 db=2，与限流/会话等其它用途隔离。
  - 连接池为类级单例，懒加载；池在第一次取用时按 settings.redis_url 创建。
  - 值统一以 JSON 字符串存储（decode_responses=True，存取均为 str）。
  - 缓存键约定：同一城市 + 同一日期 → TTL 窗口内命中同一份结果。
"""
import asyncio
import contextvars
import inspect
import json
from functools import wraps

import redis.asyncio as aioredis
import structlog

from config import settings
from monitoring.metrics import CACHE_HITS, CACHE_MISSES

logger = structlog.get_logger(__name__)

TTL_WEATHER = 6 * 3600     # 6 小时 —— 天气短期内基本稳定
TTL_POI     = 24 * 3600    # 24 小时 —— 景点信息变化慢
TTL_ROUTE   = 3600         # 1 小时 —— 路况/路线时效性较强

# 记录「最近一次被 @cached 包裹的调用是否命中缓存」，供节点层打 cache_hit 日志。
# ContextVar：节点是「直接 await」缓存函数（无 Task 边界），同一 context 内的 set
# 会在 await 返回后对调用方可见；并发请求各自的 Context 互不串扰。
_cache_hit_var: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "cache_hit", default=False
)


def last_cache_hit() -> bool:
    """读取最近一次 @cached 调用是否命中缓存（紧接 await 之后调用才有意义）。"""
    return _cache_hit_var.get()


class CacheClient:
    """异步 Redis 连接池单例（懒加载，按事件循环绑定）。

    redis.asyncio 的连接绑定在创建它的事件循环上：池一旦在某个 loop 上建立，
    换到另一个 loop（例如脚本里多次 asyncio.run、或测试反复重建 loop）再复用，
    就会抛 "got Future attached to a different loop"。因此这里记录池所属的 loop，
    发现当前 loop 变了就重建池。生产环境是单一长驻 loop，只会建一次、全程复用。
    """

    _pool: aioredis.ConnectionPool | None = None
    _loop: asyncio.AbstractEventLoop | None = None

    @classmethod
    async def get(cls) -> aioredis.Redis:
        loop = asyncio.get_running_loop()
        if cls._pool is None or cls._loop is not loop:
            # 旧池的连接绑定在已关闭的旧 loop 上，在新 loop 里 await 关闭会再次
            # 触发 "Event loop is closed"；直接丢弃引用交给 GC 即可。
            cls._pool = aioredis.ConnectionPool.from_url(
                settings.redis_url,
                db=2,
                max_connections=20,
                decode_responses=True,
            )
            cls._loop = loop
        return aioredis.Redis(connection_pool=cls._pool)


async def cache_get(key: str) -> dict | None:
    """读缓存：命中返回反序列化后的 dict，未命中返回 None。"""
    r = await CacheClient.get()
    val = await r.get(key)
    return json.loads(val) if val else None


async def cache_set(key: str, value: dict, ttl: int) -> None:
    """写缓存：JSON 序列化后带 TTL 写入（ensure_ascii=False 以保留中文）。"""
    r = await CacheClient.get()
    await r.setex(key, ttl, json.dumps(value, ensure_ascii=False))


def cached(key_template: str, ttl: int):
    """
    异步节点函数的缓存装饰器。

    key_template: 含 {arg_name} 占位符的字符串，占位符须与被装饰函数的参数同名。
        示例: @cached("weather:{city}:{date}", TTL_WEATHER)

    命中即返回缓存值；未命中则执行原函数，结果非 None 时回写缓存。
    """
    # 缓存名取键模板的首段（weather:{city}:{date} → "weather"），作为指标标签。
    cache_name = key_template.split(":", 1)[0]

    def decorator(fn):
        # 签名只在装饰时解析一次，避免每次调用重复反射。
        sig = inspect.signature(fn)

        @wraps(fn)
        async def wrapper(*args, **kwargs):
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            key = key_template.format(**bound.arguments)

            cached_val = await cache_get(key)
            if cached_val is not None:
                _cache_hit_var.set(True)
                CACHE_HITS.labels(cache=cache_name).inc()
                logger.info("cache_hit", key=key)
                return cached_val

            _cache_hit_var.set(False)
            CACHE_MISSES.labels(cache=cache_name).inc()
            logger.info("cache_miss", key=key)
            result = await fn(*args, **kwargs)
            if result is not None:
                await cache_set(key, result, ttl)
            return result

        return wrapper

    return decorator


async def get_cache_info() -> dict:
    """Redis keyspace 命中/未命中统计（供 Sprint 5 的 /health 端点使用）。"""
    r = await CacheClient.get()
    info = await r.info("stats")
    return {
        "keyspace_hits": info.get("keyspace_hits", 0),
        "keyspace_misses": info.get("keyspace_misses", 0),
    }
