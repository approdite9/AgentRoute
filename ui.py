"""Streamlit 结果渲染 —— 从 app.py 抽出的纯展示逻辑。

抽成独立模块的好处：
  1. app.py 顶层不再是一大段内联渲染，职责更清晰；
  2. 可被验证脚本（verify_ui.py）直接复用同一套渲染，便于离线检视排版，
     不必跑完整张图 / 烧高德配额。

本模块只依赖一个 `plan` dict（schemas.TravelPlan.model_dump 的形状），
不读取 st.session_state，因此任何持有 plan 的调用方都能直接渲染。
"""
from __future__ import annotations

import streamlit as st

from render import (
    _weather_icon,
    hotel_price_label,
    extract_lnglat,
    gcj02_to_wgs84,
    build_day_timeline,
    build_ical,
)


# 一行最多并排几张天气卡：再多就换行，避免长行程（如 7 天）把卡片挤成窄条、
# 文字折行错位。3~4 张在常见宽度下最稳。
_WEATHER_PER_ROW = 4


def render_plan_result(plan: dict) -> None:
    """把一份完整行程 dict 渲染为 Streamlit 页面（标题 / 往返 / 天气 / 地图 / 每日 / 预算 / 建议 / 导出）。"""
    _render_title(plan)
    _render_transports(plan.get("transports", []))
    _render_weather(plan.get("weather_info", []))
    _render_map(plan)
    _render_days(plan.get("days", []))
    _render_budget(plan.get("budget", {}))
    _render_suggestions(plan.get("overall_suggestions", ""))
    _render_export(plan)


# ==================== 往返交通 ====================

# 交通段类型 → 展示用图标与标签。
_LEG_META = {
    "outbound": ("🛫", "去程"),
    "return": ("🛬", "返程"),
    "inter_city": ("🚄", "城际"),
}


def _render_transports(transports: list[dict]) -> None:
    """渲染往返/城际交通段（用户填了出发地才有）。票价/时长为预估，仅供参考。"""
    if not transports:
        return
    st.markdown("---")
    st.markdown("##### 🚄 往返交通")
    st.caption("交通方式、时长与票价为基于距离的预估，仅供参考；请以实际购票为准。")
    for leg in transports:
        icon, label = _LEG_META.get(leg.get("kind", ""), ("🚄", "交通"))
        route = f"{leg.get('from_city', '')} → {leg.get('to_city', '')}"
        mode = leg.get("mode", "")
        dep, arr = leg.get("depart_time", ""), leg.get("arrive_time", "")
        time_str = f"{dep} → {arr}" if (dep or arr) else ""
        with st.container(border=True):
            head = f"{icon} **{label}**  {route}"
            if mode:
                head += f"  ·  {mode}"
            st.markdown(head)
            meta = [p for p in (
                leg.get("date", ""),
                f"🕘 {time_str}" if time_str else "",
                f"⏱️ {leg['duration']}" if leg.get("duration") else "",
                f"💰 {leg['price_range']}（预估）" if leg.get("price_range") else "",
            ) if p]
            if meta:
                st.caption("  |  ".join(meta))
            if leg.get("note"):
                st.caption(f"📌 {leg['note']}")


# ==================== 地图 ====================

# 按天给点位/路线上色（RGB），循环使用；酒店统一用醒目的金色以便区分。
_DAY_COLORS = [
    [33, 150, 243], [76, 175, 80], [244, 67, 54], [156, 39, 176],
    [255, 152, 0], [0, 188, 212], [121, 85, 72],
]
_HOTEL_COLOR = [255, 193, 7]


def _ordered_attractions(day: dict) -> list[dict]:
    """当天「有坐标」的景点，按 start_time 升序排列（无时间的保持原序、排在最后）。

    与「当日时间轴」一致的顺序，使地图上的连线与序号反映真实游览动线。
    """
    items = [a for a in day.get("attractions", []) if extract_lnglat(a.get("location"))]

    def _key(pair):
        idx, a = pair
        t = (a.get("start_time") or "").strip()
        try:
            hh, mm = t.split(":")
            return int(hh) * 60 + int(mm)
        except (ValueError, AttributeError):
            return 10**6 + idx  # 无时间：排到当天最后，且保持彼此原相对顺序
    return [a for _, a in sorted(enumerate(items), key=_key)]


def _compute_view(points: list[dict]) -> tuple[float, float, int]:
    """据点位包围盒算出居中坐标与合适的缩放级别（zoom-to-fit）。"""
    lats = [p["lat"] for p in points]
    lngs = [p["lng"] for p in points]
    lat0, lng0 = sum(lats) / len(lats), sum(lngs) / len(lngs)
    span = max(max(lats) - min(lats), max(lngs) - min(lngs))
    for thr, z in [(0.015, 14), (0.04, 13), (0.09, 12), (0.2, 11), (0.5, 10), (1.0, 9)]:
        if span <= thr:
            return lat0, lng0, z
    return lat0, lng0, 8


def _build_day_geo(day: dict, day_no: int) -> tuple[list[dict], list[dict], dict | None]:
    """单日地理数据：(景点点位[带序号], 路线坐标段, 酒店点位 or None)。坐标已转 WGS-84。"""
    color = _DAY_COLORS[(day_no - 1) % len(_DAY_COLORS)]
    attractions: list[dict] = []
    path_coords: list[list[float]] = []
    for seq, a in enumerate(_ordered_attractions(day), start=1):
        lng, lat = gcj02_to_wgs84(*extract_lnglat(a["location"]))
        attractions.append({
            "lng": lng, "lat": lat, "name": a.get("name", "景点"),
            "label": f"Day{day_no} · 第{seq}站", "seq": str(seq), "color": color,
        })
        path_coords.append([lng, lat])
    path = [{"path": path_coords, "color": color}] if len(path_coords) >= 2 else []

    hotel_pt = None
    hotel = day.get("hotel") or {}
    hll = extract_lnglat(hotel.get("location"))
    if hotel.get("name") and hll:
        lng, lat = gcj02_to_wgs84(*hll)
        hotel_pt = {"lng": lng, "lat": lat, "name": hotel["name"], "label": f"Day{day_no} · 🏨 酒店"}
    return attractions, path, hotel_pt


def _collect_map_points(plan: dict) -> list[dict]:
    """跨全部日期收集带坐标的景点 + 酒店点位（已转 WGS-84）的扁平列表。

    供需要「全部点位」的场景（如 st.map 兜底、测试）复用；按日筛选/连线由
    _render_map 内部基于 _build_day_geo 处理。
    """
    points: list[dict] = []
    for i, day in enumerate(plan.get("days", [])):
        day_no = day.get("day_index", i) + 1
        attractions, _, hotel_pt = _build_day_geo(day, day_no)
        points += attractions
        if hotel_pt:
            points.append(hotel_pt)
    return points


def _render_map(plan: dict) -> None:
    """行程地图：按日筛选，画出当天「酒店 + 按序号连线的景点动线」。"""
    days = plan.get("days", [])
    has_geo = any(extract_lnglat(a.get("location")) for d in days for a in d.get("attractions", [])) \
        or any(extract_lnglat((d.get("hotel") or {}).get("location")) for d in days)

    st.markdown("---")
    st.markdown("##### 🗺️ 行程地图")
    if not has_geo:
        if any(d.get("attractions") for d in days):
            st.caption("📍 本次未获取到景点坐标，暂无法绘制地图。")
        return

    # 日期筛选：默认「Day 1」，仅显示当天动线，避免多日路线混叠；另给「全部」总览。
    day_labels = [f"Day {d.get('day_index', i) + 1}" for i, d in enumerate(days)]
    options = ["🗓️ 全部"] + day_labels
    # key 带上天数，换一份天数不同的计划时自动重置，避免旧选项索引越界。
    choice = st.radio(
        "选择日期查看当天路线",
        options,
        index=1 if day_labels else 0,
        horizontal=True,
        key=f"map_day_sel_{len(days)}",
    )
    sel_idx = None if choice == "🗓️ 全部" else options.index(choice) - 1

    try:
        import pydeck as pdk
    except ImportError:
        # pydeck 缺失时退回 st.map（仅打点、无连线/序号）。
        pts = []
        rng = range(len(days)) if sel_idx is None else [sel_idx]
        for i in rng:
            attractions, _, hotel_pt = _build_day_geo(days[i], days[i].get("day_index", i) + 1)
            pts += [{"lat": p["lat"], "lon": p["lng"]} for p in attractions]
            if hotel_pt:
                pts.append({"lat": hotel_pt["lat"], "lon": hotel_pt["lng"]})
        if pts:
            st.map(pts)
        return

    # 汇总选中范围（全部 / 单日）的图层数据。
    attr_pts: list[dict] = []
    hotel_pts: list[dict] = []
    paths: list[dict] = []
    rng = range(len(days)) if sel_idx is None else [sel_idx]
    for i in rng:
        attractions, path, hotel_pt = _build_day_geo(days[i], days[i].get("day_index", i) + 1)
        attr_pts += attractions
        paths += path
        if hotel_pt:
            hotel_pts.append(hotel_pt)

    all_pts = attr_pts + hotel_pts
    if not all_pts:
        st.caption("📍 当天景点/酒店暂无坐标，无法绘制路线。")
        return

    layers = []
    if paths:
        layers.append(pdk.Layer(
            "PathLayer", paths, get_path="path", get_color="color",
            get_width=30, width_min_pixels=3, width_max_pixels=8,
            rounded=True, joint_rounded=True, cap_rounded=True, pickable=False,
        ))
    if attr_pts:
        layers.append(pdk.Layer(
            "ScatterplotLayer", attr_pts, get_position="[lng, lat]",
            get_fill_color="color", get_radius=150,
            radius_min_pixels=11, radius_max_pixels=26,
            stroked=True, get_line_color=[255, 255, 255], line_width_min_pixels=2,
            pickable=True,
        ))
        # 圈内序号 = 当天游览顺序（仅数字，确保 deck.gl 默认字体可渲染）。
        layers.append(pdk.Layer(
            "TextLayer", attr_pts, get_position="[lng, lat]", get_text="seq",
            get_size=15, get_color=[255, 255, 255],
            get_alignment_baseline="'center'", get_text_anchor="'middle'", pickable=False,
        ))
    if hotel_pts:
        layers.append(pdk.Layer(
            "ScatterplotLayer", hotel_pts, get_position="[lng, lat]",
            get_fill_color=_HOTEL_COLOR, get_radius=180,
            radius_min_pixels=12, radius_max_pixels=28,
            stroked=True, get_line_color=[120, 80, 0], line_width_min_pixels=2,
            pickable=True,
        ))

    lat0, lng0, zoom = _compute_view(all_pts)
    deck = pdk.Deck(
        layers=layers,
        initial_view_state=pdk.ViewState(latitude=lat0, longitude=lng0, zoom=zoom, pitch=0),
        tooltip={"text": "{label}\n{name}"},
        map_style=None,  # 用 deck.gl 自带 Carto 底图，无需 Mapbox token
    )
    st.pydeck_chart(deck, use_container_width=True)
    scope = "全程各日（按天着色）" if sel_idx is None else choice
    st.caption(
        f"📍 当前显示：**{scope}** ｜ ⬤ 景点（圈内数字＝当天游览顺序） · "
        "⬤ 酒店（金色） · 连线＝当天动线 ｜ 坐标已由高德 GCJ-02 校正到 WGS-84"
    )


# ==================== 标题 ====================

def _render_title(plan: dict) -> None:
    city = plan.get("city", "")
    origin = plan.get("origin_city", "")
    sd = plan.get("start_date", "")
    ed = plan.get("end_date", "")
    # 填了出发地则显示「出发地 → 目的地」，让往返一目了然。
    route = f"{origin} → {city}" if origin else city
    st.markdown(
        f'<div class="plan-title">🌴 {route}旅行计划 ｜ {sd} ~ {ed}</div>',
        unsafe_allow_html=True,
    )


# ==================== 天气 ====================

def _render_weather(weather: list[dict]) -> None:
    if not weather:
        return
    st.markdown("##### 🌤️ 天气预报")
    # 每行最多 _WEATHER_PER_ROW 张卡，超出换行（解决长行程卡片被挤窄、文字错位）。
    for start in range(0, len(weather), _WEATHER_PER_ROW):
        row = weather[start : start + _WEATHER_PER_ROW]
        cols = st.columns(_WEATHER_PER_ROW)
        for col, w in zip(cols, row):
            d = w.get("date", "")[-5:]
            di = _weather_icon(w.get("day_weather", ""))
            with col:
                st.markdown(
                    f'<div class="weather-card" style="text-align:center">'
                    f"<b>{d}</b><br>"
                    f"{di} {w.get('day_weather', '?')}<br>"
                    f"🌡️ {w.get('day_temp', '?')}°C / {w.get('night_temp', '?')}°C<br>"
                    f"💨 {w.get('wind_direction', '')}{w.get('wind_power', '')}"
                    f"</div>",
                    unsafe_allow_html=True,
                )


# ==================== 每日行程 ====================

def _render_days(days: list[dict]) -> None:
    st.markdown("---")
    st.markdown("##### 📅 每日行程")
    if not days:
        return
    tabs = st.tabs([f"Day {d.get('day_index', i) + 1}" for i, d in enumerate(days)])
    for tab, day in zip(tabs, days):
        with tab:
            _render_one_day(day)


def _render_one_day(day: dict) -> None:
    d = day.get("date", "")[-5:]
    desc = day.get("description", "")
    st.markdown(f'<div class="day-header">📅 {d}  {desc}</div>', unsafe_allow_html=True)

    # 半天逻辑标记：到达日 / 离开日在日头给出醒目提示。
    arr = day.get("arrival_time", "")
    dep = day.get("departure_time", "")
    if arr:
        st.info(f"🛬 当天预计 **{arr}** 抵达，行程从抵达后开始安排。")
    if dep:
        st.warning(f"🛫 当天 **{dep}** 返程，行程在出发前收尾（预留赶车/赶机时间）。")

    # 住宿
    hotel = day.get("hotel", {})
    if hotel.get("name"):
        price = hotel_price_label(hotel)
        rating = hotel.get("rating") or "-"
        addr = hotel.get("address", "")
        line = f"🏨 **{hotel['name']}**  ★{rating}  {price}"
        if addr:
            line += f"  |  {addr}"
        st.markdown(line)
    transport = day.get("transportation", "")
    if transport:
        st.caption(f"🚌 {transport}")

    # 活动时间轴：景点 + 三餐按时间合并排序，左侧时间徽标、右侧活动卡片。
    timeline = build_day_timeline(day)
    if not timeline:
        return
    st.markdown("**🗓️ 当日时间轴**")
    for ev in timeline:
        time_col, body_col = st.columns([1, 6])
        with time_col:
            # 时间段徽标（开始～结束），景点用蓝色、餐饮用橙色以便区分。
            color = "#1565C0" if ev["kind"] == "attraction" else "#E67E22"
            st.markdown(
                f'<div style="text-align:center;font-weight:700;color:{color};'
                f'line-height:1.3;padding-top:0.4rem">{ev["time"]}'
                f'<br><span style="font-weight:400;color:#888;font-size:0.78rem">'
                f'～{ev["end_time"]}</span></div>',
                unsafe_allow_html=True,
            )
        with body_col:
            with st.container(border=True):
                st.markdown(f"{ev['icon']} **{ev['title']}**")
                for ln in ev.get("lines", []):
                    if ln:
                        st.caption(ln)


# ==================== 预算 ====================

def _render_budget(budget: dict) -> None:
    if not budget:
        return
    st.markdown("---")
    st.markdown("##### 💰 预算汇总")
    cols = st.columns(5)
    items = [
        ("景点门票", budget.get("total_attractions", 0)),
        ("酒店住宿", budget.get("total_hotels", 0)),
        ("餐饮美食", budget.get("total_meals", 0)),
        ("交通出行", budget.get("total_transportation", 0)),
        ("📊 总计", budget.get("total", 0)),
    ]
    for col, (label, value) in zip(cols, items):
        with col:
            st.metric(label, f"¥{value:,}")


# ==================== 建议 ====================

def _render_suggestions(suggestions: str) -> None:
    if not suggestions:
        return
    st.markdown("---")
    st.markdown("##### 💡 旅行建议")
    for tip in suggestions.replace("；", ";").split(";"):
        tip = tip.strip()
        if tip:
            st.markdown(f"- {tip}")


# ==================== 导出 ====================

def build_markdown(p: dict) -> str:
    """把行程 dict 转成可下载的 Markdown。"""
    origin = p.get("origin_city", "")
    route = f"{origin} → {p.get('city', '')}" if origin else p.get("city", "")
    md = f"# 🌴 {route}旅行计划\n\n"
    md += f"**日期:** {p.get('start_date', '')} ~ {p.get('end_date', '')}\n\n"

    transports = p.get("transports", [])
    if transports:
        md += "## 🚄 往返交通（时长/票价为预估，仅供参考）\n\n"
        leg_label = {"outbound": "去程", "return": "返程", "inter_city": "城际"}
        for leg in transports:
            label = leg_label.get(leg.get("kind", ""), "交通")
            route_l = f"{leg.get('from_city', '')} → {leg.get('to_city', '')}"
            bits = [p2 for p2 in (
                leg.get("mode", ""),
                f"{leg.get('depart_time', '')}~{leg.get('arrive_time', '')}".strip("~"),
                leg.get("duration", ""),
                f"{leg['price_range']}（预估）" if leg.get("price_range") else "",
            ) if p2]
            md += f"- **{label}** {route_l}（{leg.get('date', '')}）：{'  |  '.join(bits)}\n"
        md += "\n"

    md += "## 🌤️ 天气预报\n\n"
    for w in p.get("weather_info", []):
        md += (
            f"- {w.get('date', '')[-5:]}: "
            f"{w.get('day_weather', '')}/{w.get('night_weather', '')}  "
            f"{w.get('day_temp', '')}°C~{w.get('night_temp', '')}°C  "
            f"{w.get('wind_direction', '')}{w.get('wind_power', '')}\n"
        )

    md += "\n## 📅 每日行程\n\n"
    for day in p.get("days", []):
        idx = day.get("day_index", 0) + 1
        md += f"### Day {idx} — {day.get('date', '')[-5:]}  {day.get('description', '')}\n\n"
        if day.get("arrival_time"):
            md += f"- 🛬 **{day['arrival_time']} 抵达**，行程从抵达后开始\n"
        if day.get("departure_time"):
            md += f"- 🛫 **{day['departure_time']} 返程**，行程在出发前收尾\n"
        h = day.get("hotel", {})
        if h.get("name"):
            md += f"- **住宿:** {h['name']}  ★{h.get('rating', '')}  {hotel_price_label(h)}  |  {h.get('address', '')}\n"
        md += f"- **交通:** {day.get('transportation', '')}\n"
        for a in day.get("attractions", []):
            t = "免费" if not a.get("ticket_price", 0) else f"¥{a.get('ticket_price', 0)}"
            tm = f"🕘{a['start_time']}  " if a.get("start_time") else ""
            md += f"  - {tm}**{a.get('name', '')}** ({a.get('category', '')})  ⏱️{a.get('visit_duration', 0)}分钟  {t}  |  {a.get('address', '')}\n"
        for m in day.get("meals", []):
            mt = {"breakfast": "早餐", "lunch": "午餐", "dinner": "晚餐"}
            md += f"  - {mt.get(m.get('type', ''), '餐')}: {m.get('name', '')}  ¥{m.get('estimated_cost', 0)}\n"
        md += "\n"

    b = p.get("budget", {})
    if b:
        md += "## 💰 预算汇总\n\n"
        md += "| 项目 | 金额 |\n|------|------|\n"
        md += f"| 景点门票 | ¥{b.get('total_attractions', 0):,} |\n"
        md += f"| 酒店住宿 | ¥{b.get('total_hotels', 0):,} |\n"
        md += f"| 餐饮美食 | ¥{b.get('total_meals', 0):,} |\n"
        md += f"| 交通出行 | ¥{b.get('total_transportation', 0):,} |\n"
        md += f"| **总计** | **¥{b.get('total', 0):,}** |\n"

    sug = p.get("overall_suggestions", "")
    if sug:
        md += "\n## 💡 旅行建议\n\n"
        for tip in sug.replace("；", ";").split(";"):
            tip = tip.strip()
            if tip:
                md += f"- {tip}\n"
    return md


def _render_export(plan: dict) -> None:
    st.markdown("---")
    st.markdown("##### 📥 导出计划")
    city = plan.get("city", "旅行")
    col_md, col_ics = st.columns(2)
    with col_md:
        st.download_button(
            label="📄 下载 Markdown",
            data=build_markdown(plan),
            file_name=f"{city}_旅行计划.md",
            mime="text/markdown",
            use_container_width=True,
        )
    with col_ics:
        # iCal：可直接导入手机/电脑日历，每个景点与餐饮各成一个带时间的日程。
        st.download_button(
            label="📅 导出到日历 (.ics)",
            data=build_ical(plan),
            file_name=f"{city}_旅行计划.ics",
            mime="text/calendar",
            use_container_width=True,
            help="下载后用「日历」App 打开即可把行程逐项加入日历。",
        )
