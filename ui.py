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

from render import _weather_icon, hotel_price_label


# 一行最多并排几张天气卡：再多就换行，避免长行程（如 7 天）把卡片挤成窄条、
# 文字折行错位。3~4 张在常见宽度下最稳。
_WEATHER_PER_ROW = 4


def render_plan_result(plan: dict) -> None:
    """把一份完整行程 dict 渲染为 Streamlit 页面（标题 / 天气 / 每日 / 预算 / 建议 / 导出）。"""
    _render_title(plan)
    _render_weather(plan.get("weather_info", []))
    _render_days(plan.get("days", []))
    _render_budget(plan.get("budget", {}))
    _render_suggestions(plan.get("overall_suggestions", ""))
    _render_export(plan)


# ==================== 标题 ====================

def _render_title(plan: dict) -> None:
    city = plan.get("city", "")
    sd = plan.get("start_date", "")
    ed = plan.get("end_date", "")
    st.markdown(
        f'<div class="plan-title">🌴 {city}旅行计划 ｜ {sd} ~ {ed}</div>',
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

    # 景点
    attractions = day.get("attractions", [])
    if attractions:
        st.markdown("**🏛️ 景点**")
        for a in attractions:
            ticket = a.get("ticket_price", 0)
            ts = "🆓 免费" if not ticket else f"🎫 ¥{ticket}"
            with st.container(border=True):
                meta = [a.get("name", "?")]
                if a.get("category"):
                    meta.append(a["category"])
                meta.append(f"⏱️ {a.get('visit_duration', 0)}分钟")
                meta.append(ts)
                st.markdown("  |  ".join(str(m) for m in meta))
                if a.get("address"):
                    st.caption(a["address"])
                if a.get("description"):
                    st.caption(a["description"])

    # 餐饮
    meals = day.get("meals", [])
    if meals:
        st.markdown("**🍽️ 餐饮推荐**")
        mt = {"breakfast": "🌅 早餐", "lunch": "☀️ 午餐", "dinner": "🌙 晚餐"}
        meal_cols = st.columns(len(meals))
        for col, m in zip(meal_cols, meals):
            label = mt.get(m.get("type", ""), "餐")
            with col:
                st.markdown(
                    f"*{label}*\n\n**{m.get('name', '?')}**  \n"
                    f"¥{m.get('estimated_cost', 0)}"
                )


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
    md = f"# 🌴 {p.get('city', '')}旅行计划\n\n"
    md += f"**日期:** {p.get('start_date', '')} ~ {p.get('end_date', '')}\n\n"

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
        h = day.get("hotel", {})
        if h.get("name"):
            md += f"- **住宿:** {h['name']}  ★{h.get('rating', '')}  {hotel_price_label(h)}  |  {h.get('address', '')}\n"
        md += f"- **交通:** {day.get('transportation', '')}\n"
        for a in day.get("attractions", []):
            t = "免费" if not a.get("ticket_price", 0) else f"¥{a.get('ticket_price', 0)}"
            md += f"  - **{a.get('name', '')}** ({a.get('category', '')})  ⏱️{a.get('visit_duration', 0)}分钟  {t}  |  {a.get('address', '')}\n"
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
    st.download_button(
        label="📄 下载 Markdown",
        data=build_markdown(plan),
        file_name=f"{plan.get('city', '旅行')}_旅行计划.md",
        mime="text/markdown",
        use_container_width=True,
    )
