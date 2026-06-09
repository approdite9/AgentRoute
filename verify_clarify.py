"""离线自检页：单独渲染「规划前智能追问」的问答控件（不依赖表单驱动）。

用真实 /clarify 端点对成都返回过的问题，复现 app.py 追问阶段的渲染逻辑，
便于肉眼检视 单选/多选/开放题 + 按钮的排版。运行：streamlit run verify_clarify.py
"""
import streamlit as st

st.set_page_config(page_title="追问自检", page_icon="🤔", layout="wide")
st.markdown(
    '<div style="font-size:1.4rem;font-weight:700;color:#2E7D32;text-align:center;margin:1rem 0">'
    '🤔 几个小问题，帮你把 成都 行程定制得更贴心</div>',
    unsafe_allow_html=True,
)
st.caption("回答后点「确认并生成」；也可以直接跳过。")

# 真实 /clarify 对「成都 · 美食探店/历史文化」返回的问题
questions = [
    {"id": "q0", "question": "想重点体验哪种类型的成都美食？", "kind": "multi",
     "options": ["火锅", "川菜馆", "街头小吃", "老字号名店"]},
    {"id": "q1", "question": "对哪些历史文化场所更感兴趣？", "kind": "multi",
     "options": ["武侯祠/杜甫草堂", "金沙遗址/三星堆（可安排一日游）", "宽窄巷子/锦里古街", "四川博物院"]},
    {"id": "q2", "question": "是否愿意在行程中加入本地人常去的非游客化街区或市井体验？", "kind": "single",
     "options": ["愿意", "不太感兴趣", "看具体安排"]},
    {"id": "q3", "question": "是否有特别想打卡的网红餐厅或文化地标？", "kind": "text", "options": []},
]

for q in questions:
    key = f"clarify_{q['id']}"
    kind = q.get("kind", "single")
    opts = q.get("options") or []
    if kind == "multi" and opts:
        st.multiselect(q["question"], opts, key=key)
    elif kind == "single" and opts:
        st.radio(q["question"], opts, key=key, index=None, horizontal=True)
    else:
        st.text_input(q["question"], key=key)

col_go, col_skip = st.columns([3, 1])
with col_go:
    st.button("✅ 确认并生成计划", type="primary", use_container_width=True)
with col_skip:
    st.button("⏭️ 跳过", use_container_width=True)
