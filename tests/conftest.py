"""
共享测试夹具（Sprint 9）。

约定：
  - 异步测试统一走 anyio 插件（pytest_plugins = ["anyio"]）+ ``@pytest.mark.anyio``；
    ``anyio_backend`` 固定为 asyncio（不引入 trio 依赖）。
  - Redis 隔离：原始读写测试用 **db 15**（redis_client 夹具，跑前跑后各 flush 一次）；
    缓存层（cache.client）固定 db 2、限流固定 db 4，分别由各自测试文件就地清理。
  - 不打真实大模型 / MCP：所有节点、图、API 测试均以 mock 替换「贵」调用，
    因此无需 DASHSCOPE_API_KEY 即可全绿（需要真实 key 的集成测试用 requires_api 跳过）。
"""
import os

import pytest
import redis.asyncio as aioredis

from config import settings

# anyio 插件提供 @pytest.mark.anyio 与异步夹具支持。
pytest_plugins = ["anyio"]

# 隔离用的测试库：与缓存(db2)/限流(db4)/broker(db0/1)/pubsub(db5) 都错开。
TEST_REDIS_DB = 15

# 需要真实大模型（DASHSCOPE_API_KEY）的集成测试用它跳过——本套件默认全程 mock。
requires_api = pytest.mark.skipif(
    not os.getenv("DASHSCOPE_API_KEY"),
    reason="需要 DASHSCOPE_API_KEY 的集成测试；默认跳过（单测全程 mock）。",
)


@pytest.fixture
def anyio_backend() -> str:
    """把 anyio 后端钉死在 asyncio，避免 anyio 默认还要跑一遍 trio。"""
    return "asyncio"


@pytest.fixture
async def redis_client():
    """隔离的测试 Redis 客户端（db 15）；用例前后各清空一次，互不污染。"""
    client = aioredis.from_url(settings.redis_url, db=TEST_REDIS_DB, decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


@pytest.fixture
def test_settings(monkeypatch):
    """暴露全局 settings 句柄供用例读取/覆盖。

    cache.client 固定 db2、限流固定 db4——没有「注入 db」的干净接口，故这里只把
    redis_url 钉到本机、把限流阈值钉到已知值，保证 API/缓存测试的可预期性，
    并把 LangSmith 追踪关掉（测试不应外连）。返回 settings 本体便于断言。
    """
    monkeypatch.setattr(settings, "langchain_tracing_v2", False, raising=False)
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    return settings


@pytest.fixture
def sample_state() -> dict:
    """一份 TripState 初始字典：北京、2 天行程（2026-06-01 ~ 2026-06-02）。

    含 LangGraph 全流程会读到的全部输入键与控制位，可直接喂给 graph.ainvoke
    或单个节点。hitl_enabled=False → review 节点透传、不触发 interrupt。
    """
    return {
        "city": "北京",
        "start_date": "2026-06-01",
        "end_date": "2026-06-02",
        "preferences": ["历史文化"],
        "hotel_type": "经济型",
        "transport": ["地铁"],
        "extra": "",
        "messages": [],
        "weather_data": None,
        "poi_data": None,
        "hotel_data": None,
        "route_data": None,
        "final_plan": None,
        "user_feedback": None,
        "hitl_enabled": False,
        "error": None,
        "retry_count": 0,
    }
