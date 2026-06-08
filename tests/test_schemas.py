"""
TASK B —— Pydantic v2 校验器单元测试（schemas.py + render.parse_plan）。

这些是纯函数校验，不依赖 Redis / 大模型，应 100% 通过且毫秒级。
"""
import pytest
from pydantic import ValidationError

from schemas import WeatherInfo, Budget, DayPlan, Meal, Hotel, TravelPlan
from render import parse_plan


def _weather(**over):
    base = dict(
        date="2026-06-01",
        day_weather="晴",
        night_weather="多云",
        day_temp=25.0,
        night_temp=18.0,
        wind_direction="南",
        wind_power="3级",
    )
    base.update(over)
    return WeatherInfo(**base)


def test_weather_strips_unit():
    """WeatherInfo(day_temp="25°C") → day_temp == 25.0（去单位、转 float）。"""
    w = _weather(day_temp="25°C")
    assert w.day_temp == 25.0
    assert isinstance(w.day_temp, float)


def test_weather_strips_unit_celsius_glyph():
    """同时支持 ℃（单字符摄氏度）与前后空白。"""
    w = _weather(night_temp=" 18℃ ")
    assert w.night_temp == 18.0


def test_budget_auto_total():
    """Budget 给了各分项但 total=0 → 自动汇总为各分项之和。"""
    b = Budget(
        total_attractions=100,
        total_hotels=300,
        total_meals=150,
        total_transportation=50,
        total=0,
    )
    assert b.total == 600.0


def test_budget_preset_total_preserved():
    """只给了部分分项时，显式 total（非 0）保持不变（沿用模型整体估算）。"""
    b = Budget(total_attractions=100, total=999)
    assert b.total == 999.0


def test_budget_full_breakdown_overrides_wrong_total():
    """四个分项都齐全但 total 与之不符 → 以分项之和为准，保证预算自洽。"""
    b = Budget(
        total_attractions=180,
        total_hotels=1200,
        total_meals=480,
        total_transportation=200,
        total=9999,  # 模型给了错误的总计
    )
    assert b.total == 2060.0  # 被纠正为四项之和


def test_hotel_price_range_kept():
    """Hotel.price_range 应被 schema 保留（此前 prompt 有该字段但 schema 缺失会被丢弃）。"""
    h = Hotel(name="亚龙湾酒店", price_range="800-1200元", estimated_cost=1000)
    dumped = h.model_dump()
    assert dumped["price_range"] == "800-1200元"


def test_day_ensures_three_meals():
    """DayPlan 仅给 1 餐 → 校验后补齐为三餐（早/午/晚）。"""
    d = DayPlan(date="2026-06-01", day_index=0, meals=[Meal(type="lunch", name="火锅")])
    assert len(d.meals) == 3
    assert {m.type for m in d.meals} == {"breakfast", "lunch", "dinner"}
    # 原有的午餐被保留，未被默认值覆盖。
    lunch = next(m for m in d.meals if m.type == "lunch")
    assert lunch.name == "火锅"


def test_day_no_meals_gets_three():
    """完全不给 meals 时也补齐三餐。"""
    d = DayPlan(date="2026-06-01", day_index=0)
    assert len(d.meals) == 3


def test_travel_plan_requires_days():
    """TravelPlan(days=[]) → ValidationError（days 至少 1 天）。"""
    with pytest.raises(ValidationError):
        TravelPlan(
            city="北京",
            start_date="2026-06-01",
            end_date="2026-06-02",
            days=[],
        )


def test_fallback_parse_extracts_json():
    """render.parse_plan 能从「文本 {json} 文本」中抠出 JSON 并经 schema 归一化。"""
    text = (
        'some text {"city":"北京","start_date":"2026-06-01",'
        '"end_date":"2026-06-02","days":[{"date":"2026-06-01","day_index":0}]} more text'
    )
    plan = parse_plan(text)
    assert plan is not None
    assert plan["city"] == "北京"
    assert len(plan["days"]) == 1
    # 经过 schema 归一化：每天补齐三餐。
    assert len(plan["days"][0]["meals"]) == 3


def test_parse_plan_passthrough_dict():
    """已是 dict 时原样返回（不二次解析）。"""
    d = {"city": "上海", "days": []}
    assert parse_plan(d) is d


def test_parse_plan_no_json_returns_none():
    """文本中没有 JSON 对象 → 返回 None。"""
    assert parse_plan("纯文本，没有任何花括号内容") is None
