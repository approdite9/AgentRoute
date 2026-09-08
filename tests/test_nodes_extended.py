"""
补全测试覆盖 —— synthesis_node / geocode_node / optimize_route_node / Redis 降级 / 多轮修改。

被 mock 的 seam：
  - config.Settings.create_llm → FakeLLM / FakeStructured（模拟结构化输出 + 回退路径）
  - agents.nodes._invoke_specialist → AsyncMock（数据采集节点替身）
  - mcp_client.McpClientManager.get_tools_for → 返回 FakeGeoTool（模拟 maps_geo）
  - cache.client.cache_get / cache_set → 模拟 Redis 失败（降级测试）

缓存固定 db 2；autouse 夹具每用例前清空并重置 MCP 单例。
"""
import uuid
import json

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

import config
import agents.nodes as nodes
from agents.graph import build_graph, entry_router
from agents.nodes import (
    synthesis_node,
    geocode_node,
    optimize_route_node,
    _parse_geo_location,
    _build_synthesis_input,
    _apply_feedback,
    DEFAULT_RESUME,
)
from mcp_client import McpClientManager
from schemas import TravelPlan, DayPlan, Attraction, Hotel
import redis.asyncio as aioredis

pytestmark = pytest.mark.anyio


# ==================== 测试替身 ====================

class _FakeStructured:
    """模拟 with_structured_output 返回合法 TravelPlan。"""
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


class _FakeStructuredNone:
    """模拟 with_structured_output 返回 None（触发回退路径）。"""
    async def ainvoke(self, messages):
        return None


class _FakeLLM:
    """结构化输出正常的 LLM 替身。"""
    def with_structured_output(self, schema, method=None):
        return _FakeStructured()

    async def ainvoke(self, messages):
        return MagicMock(content='{"city":"北京","start_date":"2026-06-01","end_date":"2026-06-02","days":[{"date":"2026-06-01","day_index":0},{"date":"2026-06-02","day_index":1}]}')


class _FakeLLMStructuredFails:
    """结构化输出失败、回退到纯文本解析的 LLM 替身。"""
    def with_structured_output(self, schema, method=None):
        return _FakeStructuredNone()

    async def ainvoke(self, messages):
        return MagicMock(content='{"city":"北京","start_date":"2026-06-01","end_date":"2026-06-02","days":[{"date":"2026-06-01","day_index":0},{"date":"2026-06-02","day_index":1}]}')


class _FakeGeoTool:
    """模拟 maps_geo MCP 工具。"""
    name = "maps_geo"

    async def ainvoke(self, payload):
        addr = payload.get("address", "")
        if "故宫" in addr:
            return '{"geocodes":[{"location":"116.397128,39.916527"}]}'
        elif "颐和园" in addr:
            return '{"geocodes":[{"location":"116.275436,39.999877"}]}'
        # 未知地点返回空结果
        return '{"geocodes":[]}'


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


@pytest.fixture
def sample_state() -> dict:
    return {
        "city": "北京",
        "start_date": "2026-06-01",
        "end_date": "2026-06-02",
        "preferences": ["历史文化"],
        "hotel_type": "经济型",
        "transport": ["地铁"],
        "extra": "",
        "origin_city": "",
        "arrival_time": "",
        "departure_time": "",
        "party_type": "",
        "party_size": 0,
        "budget_level": "",
        "messages": [],
        "weather_data": "晴 25°C",
        "poi_data": "故宫、天坛、颐和园",
        "hotel_data": "北京饭店 | 300-500元",
        "route_data": "故宫→天坛 地铁 40分钟",
        "rag_context": None,
        "final_plan": None,
        "user_feedback": None,
        "hitl_enabled": False,
        "error": None,
        "retry_count": 0,
    }


def _fresh(state: dict, **over) -> dict:
    s = dict(state)
    s.update(over)
    return s


# ==================== synthesis_node 测试 ====================

class TestSynthesisNode:
    """synthesis_node 的结构化输出 + 回退路径测试。"""

    async def test_structured_output_success(self, sample_state, monkeypatch):
        """正常路径：with_structured_output 成功返回 TravelPlan。"""
        monkeypatch.setattr(config.Settings, "create_llm", lambda self, **kw: _FakeLLM())

        result = await synthesis_node(sample_state)

        assert result["final_plan"] is not None
        assert result["final_plan"]["city"] == "北京"
        assert len(result["final_plan"]["days"]) == 2
        assert result.get("error") is None

    async def test_structured_output_none_triggers_fallback(self, sample_state, monkeypatch):
        """结构化输出返回 None → 触发纯文本回退路径（render.parse_plan 解析）。"""
        monkeypatch.setattr(
            config.Settings, "create_llm", lambda self, **kw: _FakeLLMStructuredFails()
        )

        result = await synthesis_node(sample_state)

        # 回退路径应成功解析出计划
        assert result["final_plan"] is not None
        assert result["final_plan"]["city"] == "北京"
        assert result.get("error") is None

    async def test_total_failure_returns_error(self, sample_state, monkeypatch):
        """LLM 完全失败 → error 有值，final_plan 为 None。"""
        class _BrokenLLM:
            def with_structured_output(self, schema, method=None):
                raise RuntimeError("LLM service unavailable")

        monkeypatch.setattr(config.Settings, "create_llm", lambda self, **kw: _BrokenLLM())

        result = await synthesis_node(sample_state)

        assert result["final_plan"] is None
        assert result.get("error") is not None
        assert "synthesize" in result["error"]

    async def test_build_synthesis_input_contains_all_data(self, sample_state):
        """_build_synthesis_input 拼装包含所有采集数据和行程参数。"""
        text = _build_synthesis_input(sample_state)

        assert "北京" in text
        assert "2026-06-01" in text
        assert "历史文化" in text
        assert "晴 25°C" in text
        assert "故宫" in text
        assert "北京饭店" in text

    async def test_apply_feedback_modification(self, sample_state):
        """多轮修改：已有 final_plan + 有效 user_feedback → 输入包含原计划和修改要求。"""
        state = _fresh(
            sample_state,
            final_plan={"city": "北京", "days": [{"date": "2026-06-01"}]},
            user_feedback="把第二天换成颐和园",
        )
        base_input = _build_synthesis_input(state)
        result = _apply_feedback(base_input, state)

        assert "原始计划 JSON" in result
        assert "最小必要修改" in result
        assert "把第二天换成颐和园" in result

    async def test_apply_feedback_default_resume_no_change(self, sample_state):
        """默认 resume 哨兵值 → 不触发修改逻辑。"""
        state = _fresh(sample_state, user_feedback=DEFAULT_RESUME)
        base_input = "base input"
        result = _apply_feedback(base_input, state)

        assert result == "base input"

    async def test_apply_feedback_first_round_supplement(self, sample_state):
        """首轮人审反馈（无 final_plan + 有效意见）→ 作为额外约束并入。"""
        state = _fresh(sample_state, user_feedback="多安排一些美食")
        base_input = "base input"
        result = _apply_feedback(base_input, state)

        assert "多安排一些美食" in result
        assert "修改意见" in result
        assert "原始计划" not in result  # 非多轮修改，无原始计划


# ==================== geocode_node 测试 ====================

class TestGeocodeNode:
    """geocode_node 补坐标的正常路径和降级路径测试。"""

    async def test_parse_geo_location_standard(self):
        """标准 maps_geo 返回格式解析。"""
        text = '{"geocodes":[{"location":"116.397128,39.916527"}]}'
        loc = _parse_geo_location(text)
        assert loc == {"longitude": 116.397128, "latitude": 39.916527}

    async def test_parse_geo_location_no_match(self):
        """无坐标信息返回 None。"""
        assert _parse_geo_location("no coordinates here") is None
        assert _parse_geo_location("{}") is None

    async def test_geocode_fills_missing_coordinates(self, sample_state, monkeypatch):
        """对缺坐标的景点成功补全经纬度。"""
        plan = {
            "city": "北京",
            "days": [{
                "date": "2026-06-01",
                "day_index": 0,
                "attractions": [
                    {"name": "故宫", "address": "东城区", "location": {}},
                    {"name": "颐和园", "address": "海淀区", "location": {}},
                ],
                "hotel": {"name": "北京饭店", "address": "王府井", "location": {}},
            }],
        }
        state = _fresh(sample_state, final_plan=plan)

        monkeypatch.setattr(
            McpClientManager, "get_tools_for",
            AsyncMock(return_value=[_FakeGeoTool()]),
        )

        result = await geocode_node(state)

        assert result.get("final_plan") is not None
        filled_plan = result["final_plan"]
        # 故宫应有坐标
        gugong = filled_plan["days"][0]["attractions"][0]
        assert gugong["location"]["longitude"] == 116.397128
        assert gugong["location"]["latitude"] == 39.916527

    async def test_geocode_skips_already_located(self, sample_state, monkeypatch):
        """已有坐标的景点不重复调用 geo 工具。"""
        plan = {
            "city": "北京",
            "days": [{
                "date": "2026-06-01",
                "day_index": 0,
                "attractions": [
                    {"name": "故宫", "address": "东城区",
                     "location": {"longitude": 116.0, "latitude": 39.0}},
                ],
                "hotel": {"name": "Hotel", "location": {"longitude": 1.0, "latitude": 1.0}},
            }],
        }
        state = _fresh(sample_state, final_plan=plan)

        # 全部已有坐标 → 不连 MCP
        result = await geocode_node(state)
        assert result == {}  # 无需更新

    async def test_geocode_no_plan_returns_empty(self, sample_state):
        """无 final_plan 直接返回空 dict。"""
        state = _fresh(sample_state, final_plan=None)
        result = await geocode_node(state)
        assert result == {}

    async def test_geocode_mcp_failure_graceful(self, sample_state, monkeypatch):
        """MCP 连接失败 → best-effort 静默返回空。"""
        plan = {
            "city": "北京",
            "days": [{"date": "2026-06-01", "day_index": 0,
                      "attractions": [{"name": "故宫", "location": {}}],
                      "hotel": {}}],
        }
        state = _fresh(sample_state, final_plan=plan)
        monkeypatch.setattr(
            McpClientManager, "get_tools_for",
            AsyncMock(side_effect=RuntimeError("MCP connection refused")),
        )

        result = await geocode_node(state)
        assert result == {}  # best-effort，不崩


# ==================== optimize_route_node 测试 ====================

class TestOptimizeRouteNode:
    """optimize_route_node 路线优化测试。"""

    async def test_optimize_reorders_attractions(self, sample_state):
        """有坐标的景点被按距离重排（至少调用了 optimize_plan_routes）。"""
        plan = {
            "city": "北京",
            "days": [{
                "date": "2026-06-01",
                "day_index": 0,
                "attractions": [
                    {"name": "颐和园", "start_time": "09:00", "visit_duration": 120,
                     "location": {"longitude": 116.275, "latitude": 39.999}},
                    {"name": "故宫", "start_time": "14:00", "visit_duration": 180,
                     "location": {"longitude": 116.397, "latitude": 39.916}},
                    {"name": "天坛", "start_time": "10:00", "visit_duration": 90,
                     "location": {"longitude": 116.410, "latitude": 39.882}},
                ],
                "hotel": {},
                "meals": [],
            }],
        }
        state = _fresh(sample_state, final_plan=plan)

        result = await optimize_route_node(state)

        # 应返回更新后的 plan（optimize_plan_routes 原地修改）
        assert result.get("final_plan") is not None

    async def test_optimize_no_plan_returns_empty(self, sample_state):
        """无 final_plan 返回空。"""
        state = _fresh(sample_state, final_plan=None)
        result = await optimize_route_node(state)
        assert result == {}

    async def test_optimize_no_days_returns_empty(self, sample_state):
        """plan 无 days 返回空。"""
        state = _fresh(sample_state, final_plan={"city": "北京", "days": []})
        result = await optimize_route_node(state)
        assert result == {}


# ==================== Redis Graceful Degradation 测试 ====================

class TestRedisGracefulDegradation:
    """验证 Redis 不可用时缓存层静默降级。"""

    async def test_cache_get_returns_none_on_redis_failure(self, monkeypatch):
        """cache_get 在 Redis 连接失败时返回 None（降级为 cache miss）。"""
        from cache.client import cache_get, CacheClient

        async def _broken_get():
            raise ConnectionError("Redis connection refused")

        monkeypatch.setattr(CacheClient, "get", staticmethod(AsyncMock(side_effect=ConnectionError("refused"))))

        result = await cache_get("any:key")
        assert result is None  # 降级，不抛异常

    async def test_cache_set_skips_on_redis_failure(self, monkeypatch):
        """cache_set 在 Redis 连接失败时静默跳过（不抛异常）。"""
        from cache.client import cache_set, CacheClient

        monkeypatch.setattr(CacheClient, "get", staticmethod(AsyncMock(side_effect=ConnectionError("refused"))))

        # 不应抛异常
        await cache_set("any:key", {"data": "test"}, ttl=60)

    async def test_get_cache_info_returns_degraded_status(self, monkeypatch):
        """get_cache_info 在 Redis 不可用时返回降级状态。"""
        from cache.client import get_cache_info, CacheClient

        monkeypatch.setattr(CacheClient, "get", staticmethod(AsyncMock(side_effect=ConnectionError("refused"))))

        info = await get_cache_info()
        assert info["status"] == "degraded"
        assert info["keyspace_hits"] == -1


# ==================== 多轮修改端到端测试 ====================

class TestMultiTurnModification:
    """多轮修改路径：entry_router → synthesize → geocode → optimize_route → END。"""

    async def test_modification_skips_data_collection(self, sample_state, monkeypatch):
        """多轮修改跳过 weather/poi/hotel/route，直接进 synthesize。"""
        monkeypatch.setattr(
            nodes, "_invoke_specialist", AsyncMock(return_value="mock data")
        )
        monkeypatch.setattr(config.Settings, "create_llm", lambda self, **kw: _FakeLLM())

        # 构造多轮修改状态：已有 final_plan + 有效 user_feedback
        state = _fresh(
            sample_state,
            city=f"多轮-{uuid.uuid4().hex[:6]}",
            final_plan={"city": "北京", "start_date": "2026-06-01", "end_date": "2026-06-02",
                        "days": [{"date": "2026-06-01", "day_index": 0}]},
            user_feedback="把景点换成长城",
        )

        graph = build_graph()
        cfg = {"configurable": {"thread_id": str(uuid.uuid4())}}

        seen_nodes = []
        async for event in graph.astream(state, config=cfg):
            seen_nodes.extend(event.keys())

        # 验证跳过了数据采集节点
        assert "weather" not in seen_nodes
        assert "poi" not in seen_nodes
        assert "hotel" not in seen_nodes
        assert "route" not in seen_nodes
        # 验证走了 synthesize 路径
        assert "synthesize" in seen_nodes

    async def test_entry_router_modification_path(self, sample_state):
        """entry_router: 有 final_plan + 有效 feedback → 返回 ['synthesize']。"""
        state = _fresh(
            sample_state,
            final_plan={"city": "北京", "days": []},
            user_feedback="改成3天行程",
        )
        result = entry_router(state)
        assert result == ["synthesize"]

    async def test_entry_router_empty_feedback_goes_full_flow(self, sample_state):
        """entry_router: 有 final_plan 但 feedback 为空 → 走完整采集流程。"""
        state = _fresh(
            sample_state,
            final_plan={"city": "北京", "days": []},
            user_feedback="",  # 空 feedback
        )
        result = entry_router(state)
        assert set(result) == {"weather", "poi", "hotel"}

    async def test_entry_router_no_plan_goes_full_flow(self, sample_state):
        """entry_router: 无 final_plan → 走完整采集流程（首轮规划）。"""
        result = entry_router(sample_state)
        assert set(result) == {"weather", "poi", "hotel"}
