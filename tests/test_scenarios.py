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


# ---- 按日动线（ui._build_day_geo / _ordered_attractions）----

def test_map_day_geo_sequences_by_start_time():
    """同一天景点按 start_time 排序并编号（1→2→3），连线坐标随之有序。"""
    day = {
        "day_index": 0,
        "attractions": [
            {"name": "晚", "start_time": "15:00", "location": {"longitude": 116.42, "latitude": 39.92}},
            {"name": "早", "start_time": "09:00", "location": {"longitude": 116.40, "latitude": 39.90}},
            {"name": "中", "start_time": "12:00", "location": {"longitude": 116.41, "latitude": 39.91}},
        ],
        "hotel": {"name": "H", "location": {"longitude": 116.43, "latitude": 39.93}},
    }
    attractions, path, hotel_pt = ui._build_day_geo(day, 1)
    # 序号按时间升序：早(1) → 中(2) → 晚(3)
    assert [a["name"] for a in attractions] == ["早", "中", "晚"]
    assert [a["seq"] for a in attractions] == ["1", "2", "3"]
    # 连线为单条 path，按同一顺序串起 3 个坐标
    assert len(path) == 1 and len(path[0]["path"]) == 3
    # 酒店独立成点（不进景点序号、不进连线）
    assert hotel_pt and hotel_pt["name"] == "H"


def test_map_day_geo_no_path_for_single_point():
    """当天只有 1 个有坐标的景点 → 不画连线（path 为空），仍有该点。"""
    day = {"day_index": 0, "attractions": [
        {"name": "独点", "location": {"longitude": 116.4, "latitude": 39.9}}]}
    attractions, path, hotel_pt = ui._build_day_geo(day, 1)
    assert len(attractions) == 1 and path == [] and hotel_pt is None


def test_map_day_geo_untimed_attractions_keep_order():
    """无 start_time 的景点保持原相对顺序、排在有时间者之后。"""
    day = {"day_index": 0, "attractions": [
        {"name": "无序A", "location": {"longitude": 116.40, "latitude": 39.90}},
        {"name": "有时间", "start_time": "10:00", "location": {"longitude": 116.41, "latitude": 39.91}},
        {"name": "无序B", "location": {"longitude": 116.42, "latitude": 39.92}},
    ]}
    attractions, _, _ = ui._build_day_geo(day, 1)
    assert [a["name"] for a in attractions] == ["有时间", "无序A", "无序B"]


# ---- 路线地理优化（render.optimize_day_route）----

def _hotel(lng, lat):
    return {"name": "H", "location": {"longitude": lng, "latitude": lat}}


def test_route_opt_eliminates_backtracking():
    """zigzag 顺序(东→西→东) → 最近邻重排为 西→东→东，并重置 start_time 递增。"""
    from render import optimize_day_route
    day = {"hotel": _hotel(116.40, 39.90), "attractions": [
        {"name": "东1", "location": {"longitude": 116.50, "latitude": 39.90}, "visit_duration": 60},
        {"name": "西", "location": {"longitude": 116.41, "latitude": 39.90}, "visit_duration": 60},
        {"name": "东2", "location": {"longitude": 116.49, "latitude": 39.90}, "visit_duration": 60},
    ]}
    optimize_day_route(day)
    names = [a["name"] for a in day["attractions"]]
    assert names == ["西", "东2", "东1"]  # 由近及远，无折返
    times = [a["start_time"] for a in day["attractions"]]
    assert times == sorted(times) and all(times)  # start_time 已重置且递增


def test_route_opt_keeps_locked_in_its_time_slot():
    """锁定景点(看日落，输入排在最前)应被放到正确时段(末尾、保留 18:00)，其余排在白天。"""
    from render import optimize_day_route
    day = {"hotel": _hotel(116.40, 39.90), "attractions": [
        {"name": "看日落", "start_time": "18:00", "time_locked": True,
         "location": {"longitude": 116.60, "latitude": 39.90}, "visit_duration": 90},
        {"name": "博物馆", "start_time": "09:00",
         "location": {"longitude": 116.41, "latitude": 39.90}, "visit_duration": 120},
        {"name": "公园", "start_time": "11:00",
         "location": {"longitude": 116.45, "latitude": 39.90}, "visit_duration": 90},
    ]}
    optimize_day_route(day)
    assert day["attractions"][-1]["name"] == "看日落"
    assert day["attractions"][-1]["start_time"] == "18:00"
    # 其余两个落在日落之前
    assert all(a["start_time"] < "18:00" for a in day["attractions"][:-1])


def test_route_opt_keyword_lock_without_field():
    """没有 time_locked 字段，但名称含「夜市」→ 关键词识别为锁定，锚到傍晚默认时段。"""
    from render import optimize_day_route
    day = {"hotel": _hotel(116.40, 39.90), "attractions": [
        {"name": "夜市", "location": {"longitude": 116.70, "latitude": 39.90}, "visit_duration": 90},
        {"name": "A景", "location": {"longitude": 116.41, "latitude": 39.90}, "visit_duration": 60},
        {"name": "B景", "location": {"longitude": 116.45, "latitude": 39.90}, "visit_duration": 60},
    ]}
    optimize_day_route(day)
    assert day["attractions"][-1]["name"] == "夜市"
    assert day["attractions"][-1]["start_time"] >= "18:00"


def test_route_opt_skips_small_days():
    """≤2 个景点无需优化，原样不动。"""
    from render import optimize_day_route
    day = {"attractions": [
        {"name": "X", "location": {"longitude": 116.4, "latitude": 39.9}},
        {"name": "Y", "location": {"longitude": 116.5, "latitude": 39.9}},
    ]}
    before = [a["name"] for a in day["attractions"]]
    optimize_day_route(day)
    assert [a["name"] for a in day["attractions"]] == before


def test_route_opt_coordless_attractions_kept():
    """无坐标景点不参与最近邻，但仍保留在结果里(排到带坐标序列之后)。"""
    from render import optimize_day_route
    day = {"hotel": _hotel(116.40, 39.90), "attractions": [
        {"name": "无坐标", "visit_duration": 60},
        {"name": "近", "location": {"longitude": 116.41, "latitude": 39.90}, "visit_duration": 60},
        {"name": "远", "location": {"longitude": 116.55, "latitude": 39.90}, "visit_duration": 60},
    ]}
    optimize_day_route(day)
    names = [a["name"] for a in day["attractions"]]
    assert set(names) == {"无坐标", "近", "远"} and len(names) == 3
    # 带坐标的按由近及远排在前
    assert names.index("近") < names.index("远")


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
async def test_geocode_node_fills_missing_coords(monkeypatch):
    """geocode 节点给缺坐标的景点/酒店用 maps_geo 补经纬度；已有坐标的跳过。"""
    from langchain_core.tools import BaseTool
    from mcp_client import McpClientManager

    class _FakeGeo(BaseTool):
        name: str = "maps_geo"
        description: str = "geo"
        def _run(self, **k): ...
        async def _arun(self, **k):
            return '{"results":[{"location":"104.041,30.665"}]}'

    monkeypatch.setattr(McpClientManager, "get_tools_for", AsyncMock(return_value=[_FakeGeo()]))
    state = {"final_plan": {"city": "成都", "days": [{
        "attractions": [
            {"name": "武侯祠"},                                              # 缺坐标 → 补
            {"name": "锦里", "location": {"longitude": 1.0, "latitude": 2.0}},  # 已有 → 跳过
        ],
        "hotel": {"name": "成都某酒店"},                                    # 缺坐标 → 补
    }]}}
    out = await nodes.geocode_node(state)
    plan = out["final_plan"]
    a0, a1 = plan["days"][0]["attractions"]
    assert a0["location"] == {"longitude": 104.041, "latitude": 30.665}    # 补上了
    assert a1["location"] == {"longitude": 1.0, "latitude": 2.0}           # 原值不动
    assert plan["days"][0]["hotel"]["location"]["longitude"] == 104.041


@pytest.mark.anyio
async def test_geocode_node_best_effort_on_failure(monkeypatch):
    """geo 工具异常 → geocode 静默跳过（返回 {}，不报错、不改计划）。"""
    from mcp_client import McpClientManager
    monkeypatch.setattr(McpClientManager, "get_tools_for",
                        AsyncMock(side_effect=RuntimeError("mcp down")))
    out = await nodes.geocode_node({"final_plan": {"city": "成都",
        "days": [{"attractions": [{"name": "武侯祠"}], "hotel": {}}]}})
    assert out == {}


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


# ---- 总体建议拆分（render.split_suggestions）----

def test_split_suggestions_strips_existing_numbering():
    """自带「1. 2. 3.」编号 + 换行的建议 → 拆成干净条目，去掉原序号（避免双重标记）。"""
    from render import split_suggestions
    text = "1. 白塔山日落最佳\n2. 手抓羊肉解腻\n3. 紫外线强备墨镜"
    assert split_suggestions(text) == ["白塔山日落最佳", "手抓羊肉解腻", "紫外线强备墨镜"]


def test_split_suggestions_mixed_separators_and_bullets():
    """兼容 ；/; 分隔，并剥掉 • / - 等项目符号。"""
    from render import split_suggestions
    assert split_suggestions("防晒；带泳衣；• 海鲜适量；- 提前预约") == \
        ["防晒", "带泳衣", "海鲜适量", "提前预约"]


def test_split_suggestions_empty():
    from render import split_suggestions
    assert split_suggestions("") == []
    assert split_suggestions(None) == []
