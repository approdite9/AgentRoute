"""离线排版自检页（不跑图、不烧高德配额）。

用 build_sample_plan 合成一份 7 天「打车」行程，分别用「修复前」（st.columns(len(weather))）
与「修复后」（ui._render_weather，每行≤4 张换行）渲染天气，便于肉眼对比长行程排版差异；
随后用 ui.render_plan_result 渲染完整结果，整体检视。

运行：streamlit run verify_ui.py
"""
import streamlit as st

st.set_page_config(page_title="排版自检", page_icon="🔍", layout="wide")

# 复用 app.py 的卡片样式
st.markdown(
    """<style>
    .plan-title { font-size: 1.4rem; font-weight: 700; color: #2E7D32; text-align: center; margin: 1rem 0; }
    .day-header { font-size: 1.1rem; font-weight: 700; color: #1565C0; border-bottom: 2px solid #BBDEFB; padding: 0.5rem 0; margin: 1rem 0 0.5rem; }
    .weather-card { background: #E3F2FD; border-radius: 10px; padding: 1rem; margin: 0.5rem 0; color: #1a1a1a; }
    .weather-card b { color: #1565C0; }
    </style>""",
    unsafe_allow_html=True,
)

from tests.eval.evaluator import build_sample_plan
import ui

# ---- 合成 7 天打车行程 ----
plan = build_sample_plan({
    "city": "三亚",
    "start_date": "2026-08-10",
    "end_date": "2026-08-16",
    "preferences": ["海滨", "度假"],
    "hotel_type": "海景度假酒店",
    "transport": ["打车/网约车"],
})
# build_sample_plan 不产出天气，这里补 7 天，触发长行程天气排版
_weathers = ["晴", "多云", "小雨", "阴", "晴", "雷阵雨", "多云"]
plan["weather_info"] = [
    {
        "date": d["date"], "day_weather": _weathers[i % len(_weathers)],
        "night_weather": "多云", "day_temp": 32 - i, "night_temp": 26,
        "wind_direction": "东南风", "wind_power": "3-4级",
    }
    for i, d in enumerate(plan["days"])
]
plan["overall_suggestions"] = "防晒；带泳衣；海鲜适量；打车高峰期提前预约"

weather = plan["weather_info"]

st.header("① 天气排版对比")

st.subheader("修复前：st.columns(len(weather)) —— 7 天 = 7 列窄条")
cols = st.columns(len(weather))
_icon_map = {"晴": "☀️", "多云": "⛅", "阴": "☁️", "小雨": "🌧️", "中雨": "🌧️", "大雨": "⛈️", "暴雨": "⛈️"}
for i, w in enumerate(weather):
    d = w.get("date", "")[-5:]
    di = _icon_map.get(w.get("day_weather", ""), "🌡️")
    with cols[i]:
        st.markdown(
            f"""<div class="weather-card" style="text-align:center">
            <b>{d}</b><br>
            {di} {w.get('day_weather', '?')}<br>
            🌡️ {w.get('day_temp', '?')}°C / {w.get('night_temp', '?')}°C<br>
            💨 {w.get('wind_direction', '')}{w.get('wind_power', '')}
            </div>""",
            unsafe_allow_html=True,
        )

st.markdown("---")
st.subheader("修复后：每行≤4 张，换行 + 复用 _weather_icon（雷阵雨也有图标）")
ui._render_weather(weather)

st.markdown("---")
if st.checkbox("② 显示完整结果渲染（ui.render_plan_result）", value=True):
    ui.render_plan_result(plan)
