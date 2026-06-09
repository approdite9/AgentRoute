"""
TASK E —— LangGraph 流水线测试（mock 掉所有「贵」调用）。

被 mock 的两个 seam：
  - agents.nodes._invoke_specialist：weather/poi/hotel/route 四个采集节点真正干活的
    入口（内部建 LLM + 取 MCP 工具 + 跑子 Agent）。用 AsyncMock 替换即可断网跑全图。
  - config.Settings.create_llm（类级）：synthesize 节点用它取整合 LLM；换成 FakeLLM，
    其 with_structured_output().ainvoke() 直接返回一个合法 TravelPlan。
    （注意：pydantic v2 不允许给 settings 实例 setattr 非字段，必须打在类上。）

缓存固定 db 2：autouse 夹具每个用例前清空，并重置 MCP 单例，避免跨用例串味。
"""
import uuid

import pytest
import redis.asyncio as aioredis
from unittest.mock import AsyncMock

import config
import agents.nodes as nodes
from agents.graph import build_graph
from agents.nodes import weather_node, poi_node, route_node
from mcp_client import McpClientManager
from schemas import TravelPlan, DayPlan

pytestmark = pytest.mark.anyio


# ==================== 测试替身 ====================

class _FakeStructured:
    async def ainvoke(self, messages):
        return TravelPlan(
            city="北京",
            start_date="2026-06-01",
            end_date="2026-06-02",
            days=[
                DayPlan(date="2026-06-01", day_index=0),
                DayPlan(date="2026-06-02", day_index=1),
            ],
        )


class _FakeLLM:
    """冒充 ChatTongyi：结构化输出直接产出合法 TravelPlan，绕开真实大模型。"""

    def with_structured_output(self, schema, method=None):
        return _FakeStructured()

    async def ainvoke(self, messages):  # 回退路径用不到，留着兜底
        return _FakeStructured().ainvoke(messages)


@pytest.fixture(autouse=True)
async def _isolate():
    """每个用例前后清空缓存库(db2)并重置 MCP 单例。"""
    async def _flush():
        client = aioredis.from_url("redis://localhost:6379", db=2, decode_responses=True)
        await client.flushdb()
        await client.aclose()

    McpClientManager.reset()
    await _flush()
    yield
    await _flush()
    McpClientManager.reset()


def _fresh_state(sample_state: dict, **over) -> dict:
    state = dict(sample_state)
    state.update(over)
    return state


def _cfg() -> dict:
    """每次跑图用独立 thread_id，避免检查点跨用例复用。"""
    return {"configurable": {"thread_id": str(uuid.uuid4())}}


# ==================== 用例 ====================

async def test_weather_node_caches(sample_state, monkeypatch):
    """同城同期连续两次 weather_node：第二次命中缓存，底层只真正调用一次。"""
    spy = AsyncMock(return_value="晴 25°C，适宜出行")
    monkeypatch.setattr(nodes, "_invoke_specialist", spy)

    # 唯一城市名确保首次必然 cache miss（与历史缓存隔离）。
    state = _fresh_state(sample_state, city=f"城市-{uuid.uuid4()}")

    first = await weather_node(state)
    second = await weather_node(state)

    assert first["weather_data"] == "晴 25°C，适宜出行"
    assert second["weather_data"] == "晴 25°C，适宜出行"
    # 第二次是缓存命中 → 底层昂贵调用总共只发生一次。
    assert spy.await_count == 1


async def test_graph_reaches_end(sample_state, monkeypatch):
    """全图跑通（节点全 mock）：最终 state 带出 final_plan。"""
    monkeypatch.setattr(
        nodes, "_invoke_specialist", AsyncMock(return_value="采集到的数据片段")
    )
    monkeypatch.setattr(config.Settings, "create_llm", lambda self, **kw: _FakeLLM())

    graph = build_graph()
    final = await graph.ainvoke(_fresh_state(sample_state), config=_cfg())

    assert final.get("final_plan") is not None
    assert final["final_plan"]["city"] == "北京"
    assert len(final["final_plan"]["days"]) == 2
    assert final.get("error") is None


async def test_error_node_on_mcp_failure(sample_state, monkeypatch):
    """MCP 取工具抛异常 → 全图最终落到 error_handler（final_plan 为空、error 有值）。"""
    # 非瞬时错误（含 INVALID_KEY）→ tenacity 不重试、快速失败，且 should_continue 直接判 error。
    monkeypatch.setattr(
        McpClientManager,
        "get_tools_for",
        AsyncMock(side_effect=RuntimeError("INVALID_KEY: mcp auth failed")),
    )

    graph = build_graph()
    state = _fresh_state(sample_state, city=f"城市-{uuid.uuid4()}")

    seen = []
    async for event in graph.astream(state, config=_cfg()):
        seen.extend(event.keys())

    assert "error_handler" in seen
    final = await graph.ainvoke(
        _fresh_state(sample_state, city=f"城市-{uuid.uuid4()}"), config=_cfg()
    )
    assert final.get("final_plan") is None
    assert final.get("error")


async def test_poi_failure_increments_count(sample_state, monkeypatch):
    """关键节点 poi 失败 → 写入 error、retry_count +1（poi 是流水线唯一的失败闸门）。"""
    monkeypatch.setattr(
        nodes, "_invoke_specialist", AsyncMock(side_effect=Exception("transient timeout"))
    )
    state = _fresh_state(sample_state, city=f"城市-{uuid.uuid4()}", retry_count=0)

    out = await poi_node(state)

    assert out["poi_data"] is None
    assert out["error"] is not None
    assert out["retry_count"] == 1  # 0 → 1


async def test_best_effort_node_soft_fails(sample_state, monkeypatch):
    """weather/route 等增补节点失败 → data=None，且**不触碰 error / 不增 retry_count**。"""
    monkeypatch.setattr(
        nodes, "_invoke_specialist",
        AsyncMock(side_effect=RuntimeError("MISSING_REQUIRED_PARAMS")),
    )
    state = _fresh_state(sample_state, city=f"城市-{uuid.uuid4()}", retry_count=0)

    out_w = await weather_node(state)
    assert out_w["weather_data"] is None
    assert "error" not in out_w          # best-effort：不写 error

    out_r = await route_node(state)
    assert out_r["route_data"] is None
    assert "error" not in out_r
    assert "retry_count" not in out_r


async def test_supplementary_failure_still_plans(sample_state, monkeypatch):
    """route 节点失败（如骑行缺参 MISSING_REQUIRED_PARAMS）→ 计划仍照常生成，不落 error_handler。

    复现用户场景：成都 + 骑行 → 路线工具失败。修好后应降级（route_data 缺失）而非整张计划失败。
    """
    def _by_domain(*, domain, **kw):
        if domain == "route":
            raise RuntimeError("MISSING_REQUIRED_PARAMS: bicycling needs origin/destination")
        return "采集到的数据片段"

    monkeypatch.setattr(nodes, "_invoke_specialist", AsyncMock(side_effect=_by_domain))
    monkeypatch.setattr(config.Settings, "create_llm", lambda self, **kw: _FakeLLM())

    graph = build_graph()
    seen = []
    async for event in graph.astream(
        _fresh_state(sample_state, city=f"城市-{uuid.uuid4()}"), config=_cfg()
    ):
        seen.extend(event.keys())

    assert "synthesize" in seen
    assert "error_handler" not in seen   # 路线失败不再搞垮整张计划
    final = await graph.ainvoke(
        _fresh_state(sample_state, city=f"城市-{uuid.uuid4()}"), config=_cfg()
    )
    assert final.get("final_plan") is not None
    assert final.get("error") is None
