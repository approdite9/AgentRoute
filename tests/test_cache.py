"""
TASK C —— Redis 缓存层测试（cache.client）。

需要本机 Redis 在跑（redis://localhost:6379）。缓存层固定使用 db 2；为避免污染，
每个用例都用 uuid 生成唯一键，且 autouse 夹具在用例前后清空 db 2 的 test:* 键。
"""
import asyncio
import uuid

import pytest
import redis.asyncio as aioredis

from config import settings
from cache.client import cache_set, cache_get, cached, last_cache_hit
from cache.keys import weather_key, poi_key, route_key

pytestmark = pytest.mark.anyio


# ==================== 键命名规范（同步，纯函数） ====================

@pytest.mark.parametrize(
    "builder, args, expected",
    [
        (weather_key, ("北京", "2026-06-01"), "weather:北京:2026-06-01"),
        (poi_key, ("北京", "历史文化"), "poi:北京:历史文化"),
        (route_key, ("故宫", "天坛", "walking"), "route:walking:故宫:天坛"),
    ],
)
async def test_cache_key_builders(builder, args, expected):
    """键构造与 cached 装饰器使用的模板保持一致。"""
    assert builder(*args) == expected


@pytest.fixture(autouse=True)
async def _clean_cache_db():
    """缓存层固定 db 2：用例前后清掉本测试写入的 test:* 键，避免相互污染。"""
    async def _purge():
        client = aioredis.from_url(settings.redis_url, db=2, decode_responses=True)
        async for key in client.scan_iter("test:*"):
            await client.delete(key)
        await client.aclose()

    await _purge()
    yield
    await _purge()


async def test_cache_set_get():
    """写入一个键再读回，应与写入值相等（含中文，验证 ensure_ascii=False）。"""
    key = f"test:{uuid.uuid4()}"
    value = {"city": "北京", "n": 3}
    await cache_set(key, value, ttl=30)
    assert await cache_get(key) == value


async def test_cache_ttl():
    """带 TTL=1 写入；等待 2s 后读取应为 None（已过期）。"""
    key = f"test:{uuid.uuid4()}"
    await cache_set(key, {"x": 1}, ttl=1)
    await asyncio.sleep(2)
    assert await cache_get(key) is None


async def test_cache_miss_returns_none():
    """读取不存在的键返回 None。"""
    assert await cache_get(f"test:{uuid.uuid4()}") is None


async def test_cached_decorator():
    """@cached 包裹的异步函数：相同参数调用两次，原函数只真正执行一次。"""
    calls = {"n": 0}
    arg = f"k-{uuid.uuid4()}"

    @cached("test:{x}", ttl=30)
    async def fetch(x: str) -> dict:
        calls["n"] += 1
        return {"value": x}

    first = await fetch(arg)
    assert last_cache_hit() is False  # 首次未命中
    second = await fetch(arg)
    assert last_cache_hit() is True  # 二次命中

    assert calls["n"] == 1  # 原函数只被真正调用一次
    assert first == second == {"value": arg}


async def test_cached_decorator_distinct_args_not_shared():
    """不同参数生成不同键，互不命中，各执行一次。"""
    calls = {"n": 0}

    @cached("test:{x}", ttl=30)
    async def fetch(x: str) -> dict:
        calls["n"] += 1
        return {"value": x}

    await fetch(f"a-{uuid.uuid4()}")
    await fetch(f"b-{uuid.uuid4()}")
    assert calls["n"] == 2
