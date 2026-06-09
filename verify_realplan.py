"""离线渲染一份**真实**行程（从 /tmp/realplan.json 读取，来自 Postgres 的 done 计划），
用以验证地图在真实数据（真实坐标）下能正常绘制。运行：streamlit run verify_realplan.py"""
import json
import streamlit as st

st.set_page_config(page_title="真实计划渲染", page_icon="🗺️", layout="wide")
st.markdown(
    """<style>
    .plan-title { font-size:1.4rem;font-weight:700;color:#2E7D32;text-align:center;margin:1rem 0; }
    .day-header { font-size:1.1rem;font-weight:700;color:#1565C0;border-bottom:2px solid #BBDEFB;padding:.5rem 0;margin:1rem 0 .5rem; }
    .weather-card { background:#E3F2FD;border-radius:10px;padding:1rem;margin:.5rem 0;color:#1a1a1a; }
    .weather-card b { color:#1565C0; }
    </style>""",
    unsafe_allow_html=True,
)

import os
import ui

# 优先读真实计划（从 Postgres 导出到 /tmp/realplan.json）；缺失则退回合成样例。
if os.path.exists("/tmp/realplan.json"):
    with open("/tmp/realplan.json", encoding="utf-8") as f:
        plan = json.load(f)
else:
    from tests.eval.evaluator import build_sample_plan
    plan = build_sample_plan({
        "city": "三亚", "start_date": "2026-08-10", "end_date": "2026-08-12",
        "preferences": ["海滨", "度假"], "transport": ["打车/网约车"],
    })

ui.render_plan_result(plan)
