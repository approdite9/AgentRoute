"""
场景测试 —— 正常场景 + 边界条件的批量自动化用例。

全部 hermetic：不连真实大模型 / 高德 MCP / 不耗配额（贵调用一律 mock）。
覆盖五块：输入解析(planner) · Schema 校验/归一化 · 地图取点(ui) · 缓存防毒化(nodes) · 图编排(graph)。

运行：pytest tests/test_scenarios.py -v
"""
import uuid

import pytest
from unittest.mock import AsyncMock

import config
import agents.nodes as nodes
import ui
from agents.planner import TripPlanner
from agents.graph import build_graph
from schemas import TravelPlan, DayPlan, Attraction, Hotel, Budget, Meal


# ==================== 测试替身 / 工具 ====================

class _FakeStructured:
    async def ainvoke(self, messages):
        return TravelPlan(
            city="测试城市", start_date="2026-06-01", end_date="2026-06-02",
            days=[DayPlan(date="2026-06-01", day_index=0),
                  DayPlan(date="2026-06-02", day_index=1)],
        )


class _FakeLLM:
    def with_structured_output(self, schema, method=None):
        return _FakeStructured()

    async def ainvoke(self, messages):
        return await _FakeStructured().ainvoke(messages)


def _parse(text: str) -> dict:
    return TripPlanner()._parse_user_input(text)


def _cfg() -> dict:
    return {"configurable": {"thread_id": str(uuid.uuid4())}}


def _fresh(sample_state: dict, **over) -> dict:
    s = dict(sample_state)
    s.update(over)
    return s


@pytest.fixture
async def _flush_cache():
    """图用例前后清空缓存库(db2)，并用唯一城市名进一步杜绝跨用例命中。"""
    import redis.asyncio as aioredis
    from mcp_client import McpClientManager
    client = aioredis.from_url("redis://localhost:6379", db=2, decode_responses=True)
    await client.flushdb()
    McpClientManager.reset()
    yield
    await client.flushdb()
    await client.aclose()
    McpClientManager.reset()


# ============================================================
# 一、输入解析（planner._parse_user_input）
# ============================================================

# ---- 正常 ----

def test_parse_full_input():
    """完整结构化需求 → 城市/起止日期/偏好/住宿/交通/额外要求全部正确抽取。"""
    text = ("成都3日游，2026年9月1日-2026年9月3日，喜欢美食探店、历史文化，"
            "住宿偏好中档型酒店，交通方式偏好骑行，额外要求: 带老人出行")
    s = _parse(text)
    assert s["city"] == "成都"
    assert s["start_date"] == "2026-09-01"
    assert s["end_date"] == "2026-09-03"
    assert s["preferences"] == ["美食探店", "历史文化"]
    assert s["hotel_type"] == "中档型酒店"
    assert s["transport"] == ["骑行"]
    assert "带老人出行" in s["extra"]


def test_parse_iso_dates():
    """日期支持 ISO 写法（2026-09-01）。"""
    s = _parse("杭州2日游，2026-09-01 至 2026-09-02，喜欢自然风光")
    assert (s["start_date"], s["end_date"]) == ("2026-09-01", "2026-09-02")
    assert s["preferences"] == ["自然风光"]


# ---- 边界 ----

def test_parse_no_dates():
    """无日期 → start/end 为空串，不报错。"""
    s = _parse("成都3日游，喜欢美食探店")
    assert s["start_date"] == "" and s["end_date"] == ""
    assert s["city"] == "成都"


def test_parse_single_date():
    """只给一个日期 → 起=止。"""
    s = _parse("成都1日游，2026年9月1日，喜欢历史文化")
    assert s["start_date"] == s["end_date"] == "2026-09-01"


def test_parse_no_preferences():
    """无偏好 → preferences 为空列表（走默认）。"""
    s = _parse("成都3日游，2026-09-01-2026-09-03")
    assert s["preferences"] == []
    assert s["transport"] == []


def test_parse_empty_input():
    """空输入 → 不崩，字段为空/默认。"""
    s = _parse("")
    assert s["city"] == ""
    assert s["preferences"] == [] and s["start_date"] == ""


def test_parse_english_city():
    """英文城市名也能从『日游』前抽取。"""
    s = _parse("Paris 5日游，喜欢艺术展览")
    assert s["city"] == "Paris"
    assert s["preferences"] == ["艺术展览"]


def test_parse_invalid_date_graceful():
    """非法日期（2 月 30 日）兜底拼接而不抛异常。"""
    s = _parse("成都2日游，2026年2月30日-2026年3月1日")
    # 起始日非法 → 兜底为 0230 形态的字符串；不应抛异常、不应为空。
    assert s["start_date"].startswith("2026-02")


# ============================================================
# 二、Schema 校验 / 归一化（schemas）
# ============================================================

def _plan(days, **over):
    base = dict(city="北京", start_date="2026-06-01", end_date="2026-06-02", days=days)
    base.update(over)
    return TravelPlan(**base)


# ---- 正常 ----

def test_schema_standard_plan_normalized():
    """标准计划：温度去单位、补三餐、预算自洽。"""
    plan = _plan(
        [DayPlan(date="2026-06-01", day_index=0,
                 attractions=[Attraction(name="故宫", ticket_price=60)],
                 meals=[Meal(type="lunch", name="火锅", estimated_cost=80)])],
        budget=Budget(total_attractions=60, total_hotels=300, total_meals=150,
                      total_transportation=40, total=0),
    ).model_dump()
    assert plan["budget"]["total"] == 550        # 自动汇总
    assert len(plan["days"][0]["meals"]) == 3     # 补齐三餐


# ---- 边界 ----

def test_schema_single_day_trip():
    """1 天行程（首尾同日）合法。"""
    plan = _plan([DayPlan(date="2026-06-01", day_index=0)])
    assert len(plan.days) == 1


def test_schema_long_trip():
    """长行程（10 天）合法，且 day_index 归一为 0..9。"""
    days = [DayPlan(date=f"2026-06-{i+1:02d}", day_index=99) for i in range(10)]
    plan = _plan(days)
    assert [d.day_index for d in plan.days] == list(range(10))


def test_schema_reindexes_day_index():
    """LLM 给的 1-based / 乱序 day_index → 按位置归一为 0-based（修复 Day2/Day3 标签）。"""
    plan = _plan([DayPlan(date="d1", day_index=1), DayPlan(date="d2", day_index=2)])
    assert [d.day_index for d in plan.days] == [0, 1]


def test_schema_rejects_empty_days():
    """days 为空 → ValidationError（至少 1 天）。"""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        _plan([])


def test_schema_budget_full_breakdown_fixes_total():
    """四项齐全但 total 不符 → 以分项之和纠正。"""
    b = Budget(total_attractions=100, total_hotels=200, total_meals=150,
               total_transportation=50, total=9999)
    assert b.total == 500


def test_schema_hotel_price_range_and_location_kept():
    """酒店 price_range / 坐标 location 不被丢弃（地图与价格展示依赖它）。"""
    h = Hotel(name="某酒店", price_range="300-500元",
              location={"longitude": 116.4, "latitude": 39.9}).model_dump()
    assert h["price_range"] == "300-500元"
    assert h["location"]["longitude"] == 116.4


# ============================================================
# 三、地图取点（ui._collect_map_points）
# ============================================================

def _map_plan(attractions, hotel=None):
    day = {"day_index": 0, "attractions": attractions, "hotel": hotel or {}}
    return {"days": [day]}


# ---- 正常 ----

def test_map_collects_attractions_and_hotel():
    """景点 + 酒店都带坐标 → 全部取到点。"""
    plan = _map_plan(
        [{"name": "A", "location": {"longitude": 116.40, "latitude": 39.90}},
         {"name": "B", "location": {"longitude": 116.41, "latitude": 39.91}}],
        hotel={"name": "H", "location": {"longitude": 116.42, "latitude": 39.92}},
    )
    pts = ui._collect_map_points(plan)
    assert len(pts) == 3
    assert all("lng" in p and "lat" in p and "name" in p for p in pts)


def test_map_string_coords_supported():
    """location 为 '经度,纬度' 字符串也能取点。"""
    pts = ui._collect_map_points(_map_plan([{"name": "A", "location": "116.40,39.90"}]))
    assert len(pts) == 1


# ---- 边界 ----

def test_map_skips_missing_or_bad_coords():
    """缺坐标 / 0,0 脏点 / 空 location 一律跳过。"""
    plan = _map_plan(
        [{"name": "无坐标"},
         {"name": "脏点", "location": {"longitude": 0, "latitude": 0}},
         {"name": "好点", "location": {"longitude": 116.4, "latitude": 39.9}}],
        hotel={"name": "无坐标酒店"},
    )
    pts = ui._collect_map_points(plan)
    assert len(pts) == 1 and pts[0]["name"] == "好点"


def test_map_empty_inputs():
    """空计划 / 无 days → 取点为空列表（不报错）。"""
    assert ui._collect_map_points({}) == []
    assert ui._collect_map_points({"days": []}) == []
    assert ui._collect_map_points(_map_plan([])) == []


def test_map_gcj02_corrected_in_china():
    """中国境内坐标取点时已做 GCJ-02→WGS-84 校正（与原始 GCJ 有数百米级偏移）。"""
    raw_lng = 116.397428
    pts = ui._collect_map_points(_map_plan(
        [{"name": "天安门", "location": {"longitude": raw_lng, "latitude": 39.90923}}]))
    assert pts and abs(pts[0]["lng"] - raw_lng) > 0.001  # 已偏移校正


# ============================================================
# 四、缓存防毒化（nodes._reject_empty / _fetch_poi）
# ============================================================

def test_reject_empty_raises_on_blank():
    """空 / 纯空白内容 → 抛错（从而不写缓存）；有内容则原样返回。"""
    with pytest.raises(ValueError):
        nodes._reject_empty("", "景点")
    with pytest.raises(ValueError):
        nodes._reject_empty("   \n ", "景点")
    assert nodes._reject_empty("岳麓书院 ...", "景点") == "岳麓书院 ..."


@pytest.mark.anyio
async def test_fetch_poi_empty_not_cached(_flush_cache, monkeypatch):
    """子 Agent 返回空 → _fetch_poi 抛错且**不写缓存**（杜绝空结果毒化 24h）。"""
    from cache.client import cache_get
    monkeypatch.setattr(nodes, "_invoke_specialist", AsyncMock(return_value=""))
    city = f"城市-{uuid.uuid4()}"

    with pytest.raises(ValueError):
        await nodes._fetch_poi(city, "历史文化")
    # 关键断言：空结果没有被写入缓存。
    assert await cache_get(f"poi:{city}:历史文化") is None


# ============================================================
# 五、图编排（graph）—— 正常各交通方式 + 边界降级/失败
# ============================================================

# ---- 正常：各交通方式都能跑通出稿 ----

@pytest.mark.anyio
@pytest.mark.parametrize("transport", [["地铁"], ["骑行"], ["打车/网约车"], ["步行"], ["自驾"], []])
async def test_graph_happy_path_all_transports(_flush_cache, sample_state, monkeypatch, transport):
    """六种交通偏好（含空）均能跑通全图、产出 final_plan。"""
    monkeypatch.setattr(nodes, "_invoke_specialist", AsyncMock(return_value="采集到的数据"))
    monkeypatch.setattr(config.Settings, "create_llm", lambda self, **kw: _FakeLLM())

    state = _fresh(sample_state, city=f"城市-{uuid.uuid4()}", transport=transport)
    final = await build_graph().ainvoke(state, config=_cfg())
    assert final.get("final_plan") is not None
    assert final.get("error") is None


# ---- 边界：增补节点失败仍出稿；关键节点失败才报错 ----

@pytest.mark.anyio
async def test_graph_route_failure_is_non_fatal(_flush_cache, sample_state, monkeypatch):
    """route 失败（如骑行缺参）→ 计划照常生成（best-effort 降级）。"""
    def _by_domain(*, domain, **kw):
        if domain == "route":
            raise RuntimeError("MISSING_REQUIRED_PARAMS")
        return "采集到的数据"
    monkeypatch.setattr(nodes, "_invoke_specialist", AsyncMock(side_effect=_by_domain))
    monkeypatch.setattr(config.Settings, "create_llm", lambda self, **kw: _FakeLLM())

    final = await build_graph().ainvoke(
        _fresh(sample_state, city=f"城市-{uuid.uuid4()}", transport=["骑行"]), config=_cfg())
    assert final.get("final_plan") is not None


@pytest.mark.anyio
async def test_graph_poi_quota_is_fatal(_flush_cache, sample_state, monkeypatch):
    """关键节点 poi 配额耗尽（非瞬时）→ 不重试、落 error_handler、无计划。"""
    monkeypatch.setattr(
        nodes, "_invoke_specialist",
        AsyncMock(side_effect=RuntimeError("USER_DAILY_QUERY_OVER_LIMIT")),
    )
    graph = build_graph()
    seen = []
    async for ev in graph.astream(
        _fresh(sample_state, city=f"城市-{uuid.uuid4()}"), config=_cfg()):
        seen.extend(ev.keys())
    assert "error_handler" in seen
    final = await graph.ainvoke(
        _fresh(sample_state, city=f"城市-{uuid.uuid4()}"), config=_cfg())
    assert final.get("final_plan") is None and final.get("error")


@pytest.mark.anyio
async def test_graph_empty_preferences_still_plans(_flush_cache, sample_state, monkeypatch):
    """无偏好 → poi 用『综合各类热门』兜底，仍能出稿。"""
    monkeypatch.setattr(nodes, "_invoke_specialist", AsyncMock(return_value="采集到的数据"))
    monkeypatch.setattr(config.Settings, "create_llm", lambda self, **kw: _FakeLLM())

    final = await build_graph().ainvoke(
        _fresh(sample_state, city=f"城市-{uuid.uuid4()}", preferences=[]), config=_cfg())
    assert final.get("final_plan") is not None


@pytest.mark.anyio
async def test_graph_populates_rag_context(_flush_cache, sample_state, monkeypatch):
    """RAG 节点在全图中运行，把"内容证据"写入 rag_context（成都有内置语料）。"""
    monkeypatch.setattr(nodes, "_invoke_specialist", AsyncMock(return_value="采集到的数据"))
    monkeypatch.setattr(config.Settings, "create_llm", lambda self, **kw: _FakeLLM())

    final = await build_graph().ainvoke(_fresh(sample_state, city="成都"), config=_cfg())
    assert final.get("final_plan") is not None
    assert final.get("rag_context")                 # 检索到攻略/口碑内容
    assert "成都" in final["rag_context"]


# ============================================================
# 六、MCP 连接切换（DashScope 托管 ↔ 高德官方，用自己的 Key 避开日配额）
# ============================================================

def test_mcp_primary_is_dashscope():
    """主路恒为 DashScope 托管 MCP（带坐标、Bearer 鉴权）。"""
    conn = config.settings.mcp_connection_primary()
    assert "dashscope" in conn["url"]
    assert conn["headers"]["Authorization"].startswith("Bearer ")


def test_mcp_fallback_enabled_only_with_amap_key(monkeypatch):
    """未设 AMAP_API_KEY → 无回退（dashscope-only）；设了 → 高德官方回退（?key=, 无 Bearer）。"""
    monkeypatch.setattr(config.settings, "amap_api_key", "")
    assert config.settings.mcp_connection_fallback() is None
    assert config.settings.mcp_provider == "dashscope-only"

    monkeypatch.setattr(config.settings, "amap_api_key", "FAKEKEY123")
    fb = config.settings.mcp_connection_fallback()
    assert fb["url"].startswith("https://mcp.amap.com/mcp") and "key=FAKEKEY123" in fb["url"]
    assert "headers" not in fb
    assert config.settings.mcp_provider == "dashscope-primary+amap-fallback"


@pytest.mark.anyio
async def test_fallback_tool_primary_then_amap():
    """_FallbackTool：主路成功不回退；主路配额/异常 → 回退到高德。"""
    from pydantic import BaseModel
    from mcp_client import _FallbackTool

    class _Args(BaseModel):
        keywords: str = ""

    def _tool(prim, fb):
        return _FallbackTool(name="maps_text_search", description="d",
                             args_schema=_Args, primary=prim, fallback=fb)

    fb = AsyncMock(); fb.ainvoke = AsyncMock(return_value="amap结果")

    ok = AsyncMock(); ok.ainvoke = AsyncMock(return_value="dashscope结果")
    assert await _tool(ok, fb)._arun(keywords="成都") == "dashscope结果"
    fb.ainvoke.assert_not_called()                       # 主路成功不回退

    quota = AsyncMock(); quota.ainvoke = AsyncMock(return_value="USER_DAILY_QUERY_OVER_LIMIT")
    assert await _tool(quota, fb)._arun(keywords="成都") == "amap结果"   # 配额文本→回退

    err = AsyncMock(); err.ainvoke = AsyncMock(side_effect=RuntimeError("boom"))
    assert await _tool(err, fb)._arun(keywords="成都") == "amap结果"     # 异常→回退


@pytest.mark.anyio
async def test_graph_rag_failure_is_non_fatal(_flush_cache, sample_state, monkeypatch):
    """RAG 检索抛错 → best-effort，计划照常生成（rag_context 缺失但不报错）。"""
    monkeypatch.setattr(nodes, "_invoke_specialist", AsyncMock(return_value="采集到的数据"))
    monkeypatch.setattr(config.Settings, "create_llm", lambda self, **kw: _FakeLLM())

    import rag.pipeline as rp
    def _boom():
        raise RuntimeError("rag store down")
    monkeypatch.setattr(rp, "get_default_pipeline", _boom)

    final = await build_graph().ainvoke(
        _fresh(sample_state, city=f"城市-{uuid.uuid4()}"), config=_cfg())
    assert final.get("final_plan") is not None      # RAG 挂了也出稿
    assert final.get("error") is None
