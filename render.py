"""渲染工具 —— JSON 解析、CLI 格式化、Streamlit 组件。"""


def parse_plan(text: str | dict) -> dict | None:
    """解析并用 Pydantic 校验旅行计划；已是 dict 则原样返回。"""
    if isinstance(text, dict):
        return text
    try:
        from schemas import TravelPlan
        import json
        import re

        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return None
        data = json.loads(match.group())
        plan = TravelPlan.model_validate(data)
        return plan.model_dump()
    except Exception:
        return None


# ==================== 共享小工具 ====================

def hotel_price_label(hotel: dict) -> str:
    """酒店价格展示：优先价格区间（price_range），无则退回单价 estimated_cost/晚。"""
    price_range = hotel.get("price_range")
    if price_range:
        return f"{price_range}/晚"
    return f"¥{hotel.get('estimated_cost', 0)}/晚"


# ==================== CLI 格式化 ====================

def _weather_icon(weather: str) -> str:
    # 先匹配更具体的描述，再退到通用的「雷/雨/雪/雾」兜底，
    # 这样「雷阵雨」「阵雨」「冻雨」等组合词也能拿到合理图标，而非落到默认温度计。
    mapping = {
        "雷阵雨": "⛈️", "雷雨": "⛈️", "暴雨": "⛈️", "大雨": "⛈️",
        "中雨": "🌧️", "小雨": "🌧️", "阵雨": "🌧️",
        "晴": "☀️", "多云": "⛅", "阴": "☁️",
        "雪": "❄️", "雾": "🌫️", "霾": "🌫️",
    }
    for key, icon in mapping.items():
        if key in weather:
            return icon
    # 通用兜底：任何含「雷」「雨」「雪」的描述都给对应图标。
    if "雷" in weather:
        return "⛈️"
    if "雨" in weather:
        return "🌧️"
    if "雪" in weather:
        return "❄️"
    return "🌡️"


def format_plan_cli(json_text: str) -> str | None:
    """将 Planner JSON 渲染为 CLI 可读的中文旅行计划。"""
    data = parse_plan(json_text)
    if data is None:
        return None

    lines = []
    city = data.get("city", "未知")
    start_date = data.get("start_date", "")
    end_date = data.get("end_date", "")

    lines.append("")
    lines.append("╔" + "═" * 58 + "╗")
    title = f"  🌴 {city} {start_date} ~ {end_date} 旅行计划"
    lines.append(f"║{title:<56}║")
    lines.append("╚" + "═" * 58 + "╝")

    # 天气
    weather_info = data.get("weather_info", [])
    if weather_info:
        lines.append("")
        lines.append("🌤️  天气概况")
        for w in weather_info:
            d = w.get("date", "")[-5:]
            di = _weather_icon(w.get("day_weather", ""))
            ni = _weather_icon(w.get("night_weather", ""))
            lines.append(
                f"   {d}  {di} {w.get('day_weather', '?')} → "
                f"{ni} {w.get('night_weather', '?')}  "
                f"{w.get('day_temp', '?')}°C / {w.get('night_temp', '?')}°C  "
                f"{w.get('wind_direction', '')}{w.get('wind_power', '')}"
            )

    # 每日行程
    for day in data.get("days", []):
        idx = day.get("day_index", 0) + 1
        d = day.get("date", "")[-5:]
        desc = day.get("description", "")
        lines.append("")
        lines.append("━" * 60)
        lines.append(f"📅 Day {idx}  {d}  {desc}")
        lines.append("━" * 60)

        hotel = day.get("hotel", {})
        if hotel.get("name"):
            hotel_line = (
                f"  🏨 {hotel['name']}  ★{hotel.get('rating', '')}  "
                f"{hotel_price_label(hotel)}"
            )
            if hotel.get("address"):
                hotel_line += f"  |  {hotel['address']}"
            lines.append(hotel_line)
        lines.append(f"  🚌 {day.get('transportation', '')}")

        attractions = day.get("attractions", [])
        if attractions:
            lines.append(f"  🏛️  景点 ({len(attractions)}个):")
            for a in attractions:
                ticket = a.get("ticket_price", 0)
                ts = "免费" if ticket == 0 else f"¥{ticket}"
                lines.append(f"     · {a.get('name', '?')}")
                # 只拼接有值的字段，避免地址/类别为空时出现悬空的「 | 」分隔符。
                meta = [
                    part for part in (
                        a.get("address", ""),
                        a.get("category", ""),
                        f"游玩约{a.get('visit_duration', 0)}分钟",
                        ts,
                    ) if part
                ]
                lines.append("       " + "  |  ".join(meta))

        meals = day.get("meals", [])
        if meals:
            lines.append("  🍽️  餐饮:")
            for m in meals:
                mt = {"breakfast": "早", "lunch": "午", "dinner": "晚"}
                label = mt.get(m.get("type", ""), "餐")
                lines.append(f"     {label} {m.get('name', '?')}  ¥{m.get('estimated_cost', 0)}")

    # 预算
    budget = data.get("budget", {})
    if budget:
        lines.append("")
        lines.append("━" * 60)
        lines.append("💰 预算汇总")
        lines.append(
            f"   景点: ¥{budget.get('total_attractions', 0):>6}  |  "
            f"酒店: ¥{budget.get('total_hotels', 0):>6}  |  "
            f"餐饮: ¥{budget.get('total_meals', 0):>6}  |  "
            f"交通: ¥{budget.get('total_transportation', 0):>6}"
        )
        lines.append(f"   📊 总计: ¥{budget.get('total', 0):,}")

    # 建议
    suggestions = data.get("overall_suggestions", "")
    if suggestions:
        lines.append("")
        lines.append("💡 旅行建议")
        for tip in suggestions.replace("；", ";").split(";"):
            tip = tip.strip()
            if tip:
                lines.append(f"   {tip}")

    lines.append("")
    return "\n".join(lines)
