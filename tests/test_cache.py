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


# ==================== 新增缓存：hotel / route / geo（节点级 @cached）====================

async def _del_keys(*keys: str) -> None:
    """删除指定 db2 键，避免新缓存（hotel/route/geo，非 test:* 前缀）污染后续用例。"""
    client = aioredis.from_url(settings.redis_url, db=2, decode_responses=True)
    for k in keys:
        await client.delete(k)
    await client.aclose()


async def test_fetch_hotel_cached(monkeypatch):
    """_fetch_hotel：相同 city+hotel_type 第二次命中缓存，底层 LLM+MCP 只调一次。"""
    from agents import nodes

    calls = {"n": 0}

    async def fake_specialist(**kwargs):
        calls["n"] += 1
        return "酒店A | 地址 | 300-500元 | 4.7"

    monkeypatch.setattr(nodes, "_invoke_specialist", fake_specialist)
    city, htype = f"缓存城A-{uuid.uuid4().hex[:6]}", "经济型"
    try:
        r1 = await nodes._fetch_hotel(city, htype)
        assert last_cache_hit() is False
        r2 = await nodes._fetch_hotel(city, htype)
        assert last_cache_hit() is True
        assert calls["n"] == 1 and r1 == r2
    finally:
        await _del_keys(f"hotel:{city}:{htype}")


async def test_fetch_route_cached_excludes_poi_from_key(monkeypatch):
    """_fetch_route：键只含 city+transport+prefs；poi 文本不同也命中（验证大文本不入键）。"""
    from agents import nodes

    calls = {"n": 0}

    async def fake_specialist(**kwargs):
        calls["n"] += 1
        return "起点→终点 | 公交 | 5km | 20分钟 | 约4元"

    monkeypatch.setattr(nodes, "_invoke_specialist", fake_specialist)
    city, tr, pr = f"缓存城R-{uuid.uuid4().hex[:6]}", "公共交通", "历史文化"
    try:
        await nodes._fetch_route(city, tr, pr, "POI 文本 1")
        await nodes._fetch_route(city, tr, pr, "完全不同的 POI 文本 2")
        assert calls["n"] == 1  # 同 city/transport/prefs → 第二次命中（poi 未入键）
        assert last_cache_hit() is True
    finally:
        await _del_keys(f"route:{city}:{tr}:{pr}")


async def test_geocode_one_cached(monkeypatch):
    """_geocode_one：相同 city+name 第二次命中缓存，maps_geo 只真正调一次。"""
    from agents import nodes

    calls = {"n": 0}

    class FakeGeo:
        async def ainvoke(self, payload):
            calls["n"] += 1
            return '{"location":"116.397128,39.916527"}'

    city, name = f"缓存城G-{uuid.uuid4().hex[:6]}", "天安门"
    try:
        loc1 = await nodes._geocode_one(city, name, FakeGeo())
        loc2 = await nodes._geocode_one(city, name, FakeGeo())
        assert calls["n"] == 1
        assert loc1 == loc2 == {"longitude": 116.397128, "latitude": 39.916527}
        assert last_cache_hit() is True
    finally:
        await _del_keys(f"geo:{city}:{name}")
