"""
FastAPI 依赖 —— 按用途复用 redis.asyncio 客户端（按 db 缓存单例）。

Redis DB 映射（project_planning.md 第 6 节）：
  DB 4  限流（sliding window）
  DB 5  Pub/Sub 流式频道
  DB 0  健康检查的 ping 直接打 broker 库即可

这些客户端绑定在 FastAPI 的事件循环上，进程长驻、全程复用一个 loop，
因此可以安全地做成模块级单例。
"""
import redis.asyncio as aioredis

from config import settings

_clients: dict[int, aioredis.Redis] = {}


def get_redis(db: int) -> aioredis.Redis:
    """获取（并缓存）指定 db 的异步 Redis 客户端。"""
    client = _clients.get(db)
    if client is None:
        client = aioredis.from_url(settings.redis_url, db=db, decode_responses=True)
        _clients[db] = client
    return client


async def close_redis() -> None:
    """关闭所有缓存的异步 Redis 客户端（应用关闭时调用）。"""
    for client in _clients.values():
        await client.aclose()
    _clients.clear()


# 语义化别名，调用处更易读。
def get_pubsub_redis() -> aioredis.Redis:
    return get_redis(5)


def get_ratelimit_redis() -> aioredis.Redis:
    return get_redis(4)


def get_health_redis() -> aioredis.Redis:
    return get_redis(0)
