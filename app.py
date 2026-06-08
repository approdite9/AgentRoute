"""智能旅行助手 — Streamlit Web 界面。"""
import asyncio
import uuid



from datetime import date

import streamlit as st

import ui

# ---- 页面配置 ----
st.set_page_config(
    page_title="智能旅行助手",
    page_icon="🧳",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---- CSS 样式 ----
st.markdown("""
<style>
    .main-header {
        font-size: 2rem; font-weight: 700; padding: 0.5rem 0;
        border-bottom: 3px solid #4A90D9; margin-bottom: 1rem;
    }
    .plan-title {
        font-size: 1.4rem; font-weight: 700; color: #2E7D32;
        text-align: center; margin: 1rem 0;
    }
    .day-header {
        font-size: 1.1rem; font-weight: 700; color: #1565C0;
        border-bottom: 2px solid #BBDEFB; padding: 0.5rem 0; margin: 1rem 0 0.5rem;
    }
    .weather-card {
        background: #E3F2FD; border-radius: 10px; padding: 1rem; margin: 0.5rem 0;
        color: #1a1a1a;
    }
    .weather-card b {
        color: #1565C0;
    }
    .budget-card {
        background: #FFF8E1; border-radius: 10px; padding: 1rem; margin: 0.5rem 0;
        color: #1a1a1a;
    }
</style>
""", unsafe_allow_html=True)


# ---- 初始化 ----
@st.cache_resource
def get_planner():
    # TripPlanner 现在是 LangGraph StateGraph 的薄封装，无需注入 LLM。
    from agents.planner import TripPlanner
    return TripPlanner()


# ---- 辅助: 构建 prompt ----
def build_prompt(
    city: str,
    start_date: date,
    end_date: date,
    transport: list[str],
    hotel_type: str,
    preferences: list[str],
    extra: str,
) -> str:
    days = (end_date - start_date).days
    parts = [
        f"{city}{days}日游",
        f"{start_date.strftime('%Y年%m月%d日')}-{end_date.strftime('%Y年%m月%d日')}",
    ]
    if preferences:
        parts.append(f"喜欢{'、'.join(preferences)}")
    if hotel_type:
        parts.append(f"住宿偏好{hotel_type}")
    if transport:
        parts.append(f"交通方式偏好{'、'.join(transport)}")
    if extra.strip():
        parts.append(f"额外要求: {extra.strip()}")
    parts.append("中等预算")
    return "，".join(parts)


# ---- 辅助: 转换流式事件 ----
_SILENCE_NAMES = {"maps_weather", "maps_text_search", "maps_search_detail",
                   "maps_direction_walking", "maps_direction_driving",
                   "maps_direction_transit_integrated", "maps_direction_bicycling",
                   "maps_distance", "maps_geo", "maps_regeocode",
                   "maps_ip_location", "maps_around_search",
                   "maps_schema_personal_map", "maps_schema_navi",
                   "maps_schema_take_taxi"}

_STREAM_LABELS = {
    "query_weather":     "🌤️ 查询天气中...",
    "search_hotel":      "🏨 搜索酒店中...",
    "search_attraction": "🏛️ 搜索景点中...",
    "maps_direction_walking_by_address":             "🚶 规划步行路线...",
    "maps_direction_driving_by_address":             "🚗 规划驾车路线...",
    "maps_direction_transit_integrated_by_address":  "🚌 规划公交路线...",
}

# ---- 主 UI ----
st.markdown('<div class="main-header">🧳 智能旅行助手</div>', unsafe_allow_html=True)

# ============ 侧边栏: 参数输入 ============
with st.sidebar:
    st.markdown("### 📋 旅行参数")

    city = st.text_input("📍 目的地城市", placeholder="例如: 杭州、成都、三亚...")

    col1, col2 = st.columns(2)
    with col1:
        start_date = st.date_input("📅 开始日期", value=date.today())
    with col2:
        end_date = st.date_input("📅 结束日期", value=date.today())

    if start_date and end_date and end_date >= start_date:
        trip_days = (end_date - start_date).days + 1
        st.info(f"📌 共计 **{trip_days}** 天")
    elif end_date < start_date:
        st.error("结束日期不能早于开始日期")

    st.markdown("---")
    st.markdown("### 🚗 交通方式")
    transport_options = ["公共交通", "自驾", "打车/网约车", "骑行", "步行"]
    transport_selected = []
    for opt in transport_options:
        if st.checkbox(opt, key=f"trans_{opt}"):
            transport_selected.append(opt)

    st.markdown("---")
    st.markdown("### 🏨 住宿偏好")
    hotel_type = st.selectbox(
        "住宿类型",
        ["不限", "经济型酒店", "中档型酒店", "豪华型酒店", "民宿/客栈", "青年旅舍"],
        index=2,
        label_visibility="collapsed",
    )
    if hotel_type == "不限":
        hotel_type = ""

    st.markdown("---")
    st.markdown("### 🎯 旅行偏好")
    pref_options = ["自然风光", "历史文化", "美食探店", "休闲度假", "艺术展览", "购物逛街", "亲子乐园"]
    pref_selected = []
    for opt in pref_options:
        if st.checkbox(opt, key=f"pref_{opt}"):
            pref_selected.append(opt)

    st.markdown("---")
    st.markdown("### 💬 额外要求")
    extra_requirements = st.text_area(
        "补充说明",
        placeholder="例如: 带老人出行需要轻松行程、想在市中心活动...",
        label_visibility="collapsed",
    )

    st.markdown("---")
    submit_btn = st.button("🚀 开始规划", type="primary", use_container_width=True)


# ============ 主区域: 结果展示 ============
if "plan_data" not in st.session_state:
    st.session_state.plan_data = None
if "plan_raw" not in st.session_state:
    st.session_state.plan_raw = ""
# 两阶段流程状态机：input（待输入）→ review（人审数据预览）→ done（已出稿）
if "phase" not in st.session_state:
    st.session_state.phase = "input"
if "draft" not in st.session_state:
    st.session_state.draft = None
# 每次「开始规划」会新建一个 thread_id（见下方提交逻辑），用于 Redis 检查点隔离对话
if "thread_id" not in st.session_state:
    st.session_state.thread_id = str(uuid.uuid4())

# 未开始时的引导页
if st.session_state.phase == "input" and not submit_btn:
    st.info("👈 在左侧填写旅行参数，然后点击 **开始规划** 按钮")
    col_a, col_b, col_c = st.columns(3)
    with col_a:
        st.markdown("##### 🌤️ 实时天气查询")
        st.caption("接入高德地图 MCP，获取目的地准确天气预报")
    with col_b:
        st.markdown("##### 🏛️ 智能景点推荐")
        st.caption("根据你的偏好，AI 精准匹配最适合的景点和路线")
    with col_c:
        st.markdown("##### 📊 预算自动汇总")
        st.caption("景点门票、餐饮、住宿、交通费用一目了然")

# 点击按钮后执行（Phase 1：采集数据 → 停在人审断点）
if submit_btn:
    if not city.strip():
        st.error("请输入目的地城市")
    elif end_date < start_date:
        st.error("结束日期不能早于开始日期")
    else:
        planner = get_planner()
        prompt = build_prompt(
            city, start_date, end_date,
            transport_selected, hotel_type, pref_selected, extra_requirements,
        )

        # 每次「开始规划」都用全新 thread_id：避免复用上一次行程的检查点，
        # 否则入口路由会因残留的 final_plan 把这次当成「修改」而跳过数据采集。
        st.session_state.thread_id = str(uuid.uuid4())
        st.session_state.plan_data = None
        st.session_state.plan_raw = prompt

        with st.spinner("🤖 正在收集数据（天气 → 景点 → 酒店 → 路线）..."):
            review = asyncio.run(
                planner.start_review(prompt, thread_id=st.session_state.thread_id)
            )

        if review.get("status") == "review":
            # 命中人审断点：保存草稿，进入 review 阶段，等用户确认 / 给意见
            st.session_state.draft = review["draft"]
            st.session_state.draft_prompt = review.get("prompt", "")
            st.session_state.phase = "review"
            st.rerun()
        else:
            # 兜底：未触发断点（异常情况）直接拿到成稿
            plan = review.get("plan") or None
            if not plan:
                st.error("😟 规划失败，请稍后重试或调整旅行参数。")
            else:
                st.session_state.plan_data = plan
                st.session_state.phase = "done"
                st.rerun()


# ============ Phase 2: 人审阶段（数据预览 + 收集反馈 → 恢复生成）============
if st.session_state.phase == "review" and st.session_state.draft:
    planner = get_planner()
    draft = st.session_state.draft

    st.markdown(
        f'<div class="plan-title">📝 {draft.get("city", "")} 数据收集预览 ｜ '
        f'{draft.get("start_date", "")} ~ {draft.get("end_date", "")}</div>',
        unsafe_allow_html=True,
    )
    st.caption(
        st.session_state.get("draft_prompt")
        or "以下数据已采集完成。确认无误可直接生成完整计划；也可填写修改意见后再生成。"
    )

    with st.expander("🔍 数据收集预览", expanded=True):
        _draft_labels = {
            "weather": "🌤️ 天气",
            "poi": "🏛️ 景点",
            "hotel": "🏨 酒店",
            "route": "🚌 路线",
        }
        for _key, _label in _draft_labels.items():
            info = draft.get(_key) or {}
            if info.get("ready"):
                st.markdown(f"**{_label}** ✅  &nbsp;_{info.get('chars', 0)} 字_", unsafe_allow_html=True)
                if info.get("preview"):
                    st.caption(info["preview"])
            else:
                st.markdown(f"**{_label}** ⚠️ 未获取到数据")

    st.text_input(
        "修改意见（留空则直接生成）",
        key="review_feedback",
        placeholder="例如：景点多安排些户外的；酒店换到市中心；行程节奏放慢些",
    )
    col_confirm, col_restart = st.columns([3, 1])
    with col_confirm:
        if st.button("✅ 确认生成", type="primary", use_container_width=True):
            # 留空 → 默认哨兵「请继续生成完整计划」（与 nodes.DEFAULT_RESUME 一致），
            # 表示无修改、直接整合。
            feedback = (st.session_state.get("review_feedback") or "").strip() or "请继续生成完整计划"
            with st.spinner("🧩 正在整合完整行程..."):
                plan = asyncio.run(
                    planner.resume(feedback, thread_id=st.session_state.thread_id)
                )
            if not plan:
                st.error("😟 生成失败，请重试或重新开始。")
            else:
                st.session_state.plan_data = plan
                st.session_state.draft = None
                st.session_state.phase = "done"
                st.session_state.status_lines = [
                    "🌤️ 天气查询", "🏛️ 景点搜索", "🏨 酒店搜索",
                    "🚌 路线规划", "🧩 行程整合",
                ]
                st.rerun()
    with col_restart:
        if st.button("↩️ 重新开始", use_container_width=True):
            st.session_state.phase = "input"
            st.session_state.draft = None
            st.session_state.plan_data = None
            st.rerun()


# ============ 结果展示 ============
plan = st.session_state.plan_data
if plan is not None:
    # 显示状态
    if "status_lines" in st.session_state and st.session_state.status_lines:
        with st.expander("🔍 规划过程", expanded=False):
            st.markdown("\n".join(st.session_state.status_lines))

    # 标题 / 天气 / 每日行程 / 预算 / 建议 / 导出 —— 统一交给 ui.render_plan_result，
    # 与 verify_ui 自检页共用同一套渲染（含天气卡换行、价格区间、预算自洽等修复）。
    ui.render_plan_result(plan)

    # ============ 多轮修改（复用同一 thread 的检查点，直接重整合）============
    st.markdown("---")
    st.markdown("### 🔄 修改计划")
    st.caption("在已生成的计划基础上微调，无需重新采集数据。")
    modification = st.text_input(
        "输入修改要求",
        key="modify_input",
        placeholder="例如：把第二天改成去博物馆，减少购物",
    )
    if st.button("应用修改", use_container_width=True) and modification.strip():
        planner = get_planner()
        with st.spinner("🧩 正在按你的要求调整行程..."):
            new_plan = asyncio.run(
                planner.modify(modification.strip(), thread_id=st.session_state.thread_id)
            )
        if new_plan:
            st.session_state.plan_data = new_plan
            st.rerun()
        else:
            st.error("😟 修改失败，请重试。")
