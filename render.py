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


# ==================== 活动时间轴 ====================

# 三餐的默认时段与展示信息（LLM 未给景点 start_time 时也能排出合理时间轴）。
_MEAL_META = {
    "breakfast": ("🌅", "早餐", "08:00"),
    "lunch": ("☀️", "午餐", "12:00"),
    "dinner": ("🌙", "晚餐", "18:00"),
}


def _time_to_minutes(t: str) -> int | None:
    """"HH:MM" → 自 0 点起的分钟数；非法/空返回 None。"""
    try:
        hh, mm = str(t).split(":")
        h, m = int(hh), int(mm)
    except (ValueError, AttributeError):
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h * 60 + m


def _minutes_to_time(mins: int) -> str:
    mins = max(0, min(mins, 23 * 60 + 59))
    return f"{mins // 60:02d}:{mins % 60:02d}"


def build_day_timeline(day: dict) -> list[dict]:
    """把一天的景点 + 三餐合并成按时间排序的「活动时间轴」事件列表（纯函数，无 UI 依赖）。

    每个事件：{time, minutes, end_time, icon, kind, title, lines}
      - 景点：优先用 schema 的 start_time；缺失则按「上一项结束 + 30 分钟缓冲」从 09:00 顺推兜底，
        保证即便 LLM 没排时间也能展示一条合理的时间轴。
      - 三餐：用 _MEAL_META 的默认时段（早 08:00 / 午 12:00 / 晚 18:00）。
    供 ui.py 渲染时间轴、render.build_ical 生成日历事件复用。
    """
    events: list[dict] = []

    # 缺省起始 09:00；到达日则从「抵达时间 + 60 分钟缓冲」起算，让无 start_time 的景点
    # 也不会被排到抵达之前。
    cursor = 9 * 60
    arr = _time_to_minutes(day.get("arrival_time", ""))
    if arr is not None:
        cursor = max(cursor, arr + 60)
    for a in day.get("attractions", []):
        if not a.get("name"):
            continue
        mins = _time_to_minutes(a.get("start_time", ""))
        if mins is None:
            mins = cursor
        dur = a.get("visit_duration") or 60
        cursor = mins + dur + 30  # 下一项的默认开始 = 本项结束 + 30 分钟缓冲
        ticket = a.get("ticket_price", 0)
        meta = []
        if a.get("category"):
            meta.append(a["category"])
        meta.append(f"⏱️ {a.get('visit_duration', 0)}分钟")
        meta.append("🆓 免费" if not ticket else f"🎫 ¥{ticket}")
        lines = ["  |  ".join(str(m) for m in meta)]
        if a.get("address"):
            lines.append(a["address"])
        if a.get("description"):
            lines.append(a["description"])
        events.append({
            "time": _minutes_to_time(mins),
            "minutes": mins,
            "end_time": _minutes_to_time(mins + dur),
            "icon": "🏛️",
            "kind": "attraction",
            "title": a.get("name", "景点"),
            "lines": lines,
        })

    for m in day.get("meals", []):
        icon, label, default_t = _MEAL_META.get(m.get("type", ""), ("🍽️", "用餐", "12:00"))
        mins = _time_to_minutes(default_t) or 12 * 60
        cost = m.get("estimated_cost", 0)
        lines = []
        title = f"{label} · {m.get('name', '自选')}"
        detail = []
        if m.get("description"):
            detail.append(m["description"])
        detail.append(f"¥{cost}")
        lines.append("  |  ".join(detail))
        events.append({
            "time": default_t,
            "minutes": mins,
            "end_time": _minutes_to_time(mins + 60),
            "icon": icon,
            "kind": "meal",
            "title": title,
            "lines": lines,
        })

    events.sort(key=lambda e: e["minutes"])
    return events


# ==================== 路线地理优化（几何最近邻）====================

# 时段绑定关键词：命中则视为「锁定时段」，路线优化不挪动（即便 LLM 没设 time_locked）。
_LOCK_KEYWORDS = (
    "日落", "日出", "夕阳", "晚霞", "夜景", "夜市", "灯光秀",
    "演出", "表演", "星空", "跨年", "晚会", "烟花",
)


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """两点（经度, 纬度）间的大圆距离（km）。用直线距离近似道路距离做相对排序。"""
    import math

    lng1, lat1 = a
    lng2, lat2 = b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def _is_time_locked(a: dict) -> bool:
    """景点是否锁定时段：显式 time_locked，或名称/类别/描述命中时段关键词。"""
    if a.get("time_locked"):
        return True
    text = (a.get("name", "") or "") + (a.get("category", "") or "") + (a.get("description", "") or "")
    return any(k in text for k in _LOCK_KEYWORDS)


def _locked_anchor_minutes(a: dict) -> int:
    """锁定景点的时间锚点：优先用其 start_time；否则按关键词给默认（日出晨间，其余傍晚）。"""
    t = _time_to_minutes(a.get("start_time", ""))
    if t is not None:
        return t
    text = (a.get("name", "") or "") + (a.get("description", "") or "")
    if any(k in text for k in ("日出", "清晨", "晨")):
        return 7 * 60
    return 18 * 60 + 30  # 日落/夜场默认锚到 18:30


def _nearest_neighbor(attrs: list[dict], start: tuple[float, float]) -> list[dict]:
    """从 start 出发，对带坐标的景点做最近邻排序，得到一条尽量不绕路的访问顺序。"""
    n = len(attrs)
    used = [False] * n
    order: list[dict] = []
    cur = start
    for _ in range(n):
        best, best_d = -1, float("inf")
        for i, a in enumerate(attrs):
            if used[i]:
                continue
            d = _haversine_km(cur, extract_lnglat(a["location"]))
            if d < best_d:
                best, best_d = i, d
        used[best] = True
        order.append(attrs[best])
        cur = extract_lnglat(attrs[best]["location"])
    return order


def optimize_day_route(day: dict) -> dict:
    """对单日景点做几何最近邻重排并重置 start_time，消除来回绕路（原地修改并返回 day）。

    规则：
      - 可动景点 = 有坐标 且 未锁定时段；其余（锁定 / 无坐标）钉在原序号位置不动。
      - 可动景点以「酒店坐标（无则第一个可动景点）」为起点做最近邻排序后填回空位。
      - 重排时间：锁定景点保留其时段锚点；可动景点按时钟顺推（每站 + 游览时长 + 30min 缓冲，
        并跳过午餐窗口 11:30–13:00 避免与午餐叠时）。
    可动景点不足 2 个时直接返回（无可优化）。
    """
    attrs = day.get("attractions") or []
    if len(attrs) < 3:
        return day

    geo_movable = [
        a for a in attrs
        if extract_lnglat(a.get("location")) and not _is_time_locked(a)
    ]
    if len(geo_movable) < 2:
        return day  # 可重排的带坐标景点不足 2 个，优化无意义

    locked = [a for a in attrs if _is_time_locked(a)]
    coordless = [
        a for a in attrs
        if not extract_lnglat(a.get("location")) and not _is_time_locked(a)
    ]

    # 1) 可动景点按最近邻排序（以酒店为起点，无则第一个可动景点）。
    hotel_ll = extract_lnglat((day.get("hotel") or {}).get("location"))
    start = hotel_ll or extract_lnglat(geo_movable[0]["location"])
    queue = _nearest_neighbor(geo_movable, start) + coordless  # 无坐标的排在最近邻序列之后

    # 2) 锁定景点先确定各自的时间锚点，并按时间排好作为「钉子」。
    anchors = sorted(locked, key=_locked_anchor_minutes)
    for a in anchors:
        a["start_time"] = _minutes_to_time(_locked_anchor_minutes(a))

    # 3) 按时钟顺排可动景点；当继续排会撞上下一个锁定锚点时，先把锚点放进去，
    #    从而让锁定景点落在正确时段、其余景点合理地分布在它前后（而非被挤到深夜）。
    day_start = _time_to_minutes(day.get("arrival_time", ""))
    clock = (day_start + 60) if day_start is not None else 9 * 60
    final: list[dict] = []
    ai = 0
    for a in queue:
        dur = a.get("visit_duration") or 60
        while ai < len(anchors):
            anc = anchors[ai]
            anc_t = _time_to_minutes(anc["start_time"]) or 0
            if clock + dur > anc_t:  # 这个可动景点会侵入锚点时段 → 先安置锚点
                final.append(anc)
                clock = max(clock, anc_t) + (anc.get("visit_duration") or 60) + 30
                ai += 1
            else:
                break
        if 11 * 60 + 30 <= clock < 13 * 60:  # 跳过午餐窗口
            clock = 13 * 60
        a["start_time"] = _minutes_to_time(clock)
        final.append(a)
        clock += dur + 30
    final.extend(anchors[ai:])  # 余下锚点（比所有可动景点都晚）补在末尾

    day["attractions"] = final
    return day


def optimize_plan_routes(plan: dict) -> dict:
    """对整份计划逐日做几何最近邻路线优化（原地修改并返回 plan）。"""
    for day in plan.get("days", []):
        optimize_day_route(day)
    return plan


# ==================== iCal（.ics）导出 ====================

def _ics_escape(text: str) -> str:
    """转义 iCal 文本值中的特殊字符（逗号/分号/反斜杠/换行）。"""
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _ics_dt(date_str: str, time_str: str) -> str | None:
    """把 "YYYY-MM-DD" + "HH:MM" 拼成 iCal 本地浮动时间 "YYYYMMDDTHHMMSS"。"""
    digits = "".join(c for c in str(date_str) if c.isdigit())
    if len(digits) != 8:
        return None
    mins = _time_to_minutes(time_str)
    if mins is None:
        return None
    return f"{digits}T{mins // 60:02d}{mins % 60:02d}00"


def build_ical(plan: dict) -> str:
    """把行程导出为 iCal（.ics）：每个景点/餐饮各生成一个 VEVENT，可直接导入手机日历。

    用本地浮动时间（不带时区后缀），各日历应用按设备本地时区解释，省去 VTIMEZONE 复杂度。
    依赖 build_day_timeline 计算每个事件的开始/结束时间，与页面时间轴保持一致。
    """
    city = plan.get("city", "旅行")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Travel Agent//Trip Planner//CN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_ics_escape(city)}旅行计划",
    ]
    seq = 0

    # 往返/城际交通段：各生成一个带出发→到达时间的日程（放在对应日期）。
    for leg in plan.get("transports", []):
        date_str = leg.get("date", "")
        dep, arr = leg.get("depart_time", ""), leg.get("arrive_time", "")
        dtstart = _ics_dt(date_str, dep)
        if not dtstart:
            continue
        dtend = _ics_dt(date_str, arr) or dtstart
        seq += 1
        digits = "".join(c for c in date_str if c.isdigit())
        route = f"{leg.get('from_city', '')}→{leg.get('to_city', '')}"
        mode = leg.get("mode", "") or "交通"
        kind_icon = {"outbound": "🛫", "return": "🛬", "inter_city": "🚄"}.get(leg.get("kind", ""), "🚄")
        desc_bits = [b for b in (leg.get("duration", ""), leg.get("price_range", ""), leg.get("note", "")) if b]
        lines += [
            "BEGIN:VEVENT",
            f"UID:{digits}-leg{seq}@travel-agent",
            f"DTSTART:{dtstart}",
            f"DTEND:{dtend}",
            f"SUMMARY:{_ics_escape(f'{kind_icon} {mode} {route}')}",
        ]
        if desc_bits:
            lines.append(f"DESCRIPTION:{_ics_escape('  '.join(desc_bits))}")
        lines.append("END:VEVENT")

    for day in plan.get("days", []):
        date_str = day.get("date", "")
        if not date_str:
            continue
        for ev in build_day_timeline(day):
            dtstart = _ics_dt(date_str, ev["time"])
            dtend = _ics_dt(date_str, ev["end_time"])
            if not dtstart:
                continue
            seq += 1
            digits = "".join(c for c in date_str if c.isdigit())
            uid = f"{digits}-{seq}@travel-agent"
            summary = f"{ev['icon']} {ev['title']}"
            desc = " ".join(" ".join(ln.split()) for ln in ev.get("lines", []))
            lines += [
                "BEGIN:VEVENT",
                f"UID:{uid}",
                f"DTSTART:{dtstart}",
                f"DTEND:{dtend or dtstart}",
                f"SUMMARY:{_ics_escape(summary)}",
            ]
            if desc:
                lines.append(f"DESCRIPTION:{_ics_escape(desc)}")
            lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    # iCal 规范要求 CRLF 行结束。
    return "\r\n".join(lines) + "\r\n"


# ==================== 共享小工具 ====================

def hotel_price_label(hotel: dict) -> str:
    """酒店价格展示：优先价格区间（price_range），无则退回单价 estimated_cost/晚。"""
    price_range = hotel.get("price_range")
    if price_range:
        return f"{price_range}/晚"
    return f"¥{hotel.get('estimated_cost', 0)}/晚"


# ---- 坐标工具：高德返回 GCJ-02，Mapbox/deck.gl 底图是 WGS-84，直接打点会偏移约 500m ----

def extract_lnglat(location) -> tuple[float, float] | None:
    """从 schema 的 location 字段抽出 (经度, 纬度)。

    兼容三种形态：dict{longitude,latitude} / dict{lng,lat} / 字符串 "经度,纬度"。
    缺失或非法时返回 None（调用方据此跳过该点）。
    """
    lng = lat = None
    if isinstance(location, dict):
        lng = location.get("longitude", location.get("lng"))
        lat = location.get("latitude", location.get("lat"))
    elif isinstance(location, str) and "," in location:
        parts = location.split(",")
        if len(parts) == 2:
            lng, lat = parts[0], parts[1]
    try:
        lng, lat = float(lng), float(lat)
    except (TypeError, ValueError):
        return None
    # 经纬度合法性粗校验（排除 0,0 之类的脏数据）。
    if not (-180 <= lng <= 180 and -90 <= lat <= 90) or (lng == 0 and lat == 0):
        return None
    return lng, lat


def gcj02_to_wgs84(lng: float, lat: float) -> tuple[float, float]:
    """高德 GCJ-02 坐标 → WGS-84（近似逆变换，误差约 1-2m，绘图足够）。

    中国境外坐标不做偏移（GCJ-02 仅在中国大陆有意义）。
    """
    import math

    if not (73.66 < lng < 135.05 and 3.86 < lat < 53.55):  # 境外
        return lng, lat
    a = 6378245.0
    ee = 0.00669342162296594323
    x, y = lng - 105.0, lat - 35.0
    d_lat = (
        -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y
        + 0.2 * math.sqrt(abs(x))
        + (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
        + (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
        + (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    )
    d_lng = (
        300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y
        + 0.1 * math.sqrt(abs(x))
        + (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
        + (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
        + (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    )
    rad_lat = lat / 180.0 * math.pi
    magic = math.sin(rad_lat)
    magic = 1 - ee * magic * magic
    sqrt_magic = math.sqrt(magic)
    d_lat = (d_lat * 180.0) / ((a * (1 - ee)) / (magic * sqrt_magic) * math.pi)
    d_lng = (d_lng * 180.0) / (a / sqrt_magic * math.cos(rad_lat) * math.pi)
    return lng - d_lng, lat - d_lat


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
                tm = f"{a['start_time']}  " if a.get("start_time") else ""
                lines.append(f"     · {tm}{a.get('name', '?')}")
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
