"""智能旅行助手 — Streamlit Web 界面（FastAPI 后端的瘦客户端）。

不再在进程内直接跑 LangGraph：提交参数后 POST 给 FastAPI（/api/v1/trips），
再订阅 SSE（/api/v1/trips/{task_id}/stream）实时展示规划进度，最终用
ui.render_plan_result 渲染。这样「UI 提交」与「API 提交」共用同一条
Celery → Postgres → Flower 链路——每次提交都会落库、可在历史/监控里看到。

后端地址由环境变量 API_BASE_URL 指定（本机默认 http://localhost:8000；
docker-compose 里覆盖为 http://api:8000）。
"""
import json
import os
from datetime import date

import httpx
import streamlit as st

import ui

API_BASE = os.environ.get("API_BASE_URL", "http://localhost:8000").rstrip("/")
USER_ID = "streamlit"  # 标记 UI 提交，便于在 /trips/history 里按用户过滤

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
    .weather-card b { color: #1565C0; }
    .budget-card {
        background: #FFF8E1; border-radius: 10px; padding: 1rem; margin: 0.5rem 0;
        color: #1a1a1a;
    }
</style>
""", unsafe_allow_html=True)


# MCP 工具名 → 进度条上的友好标签（SSE 的 tool_start 事件里带的是高德工具名）。
_TOOL_LABELS = {
    "maps_weather": "🌤️ 查询天气",
    "maps_text_search": "🔍 搜索景点 / 酒店",
    "maps_direction_walking": "🚶 规划步行路线",
    "maps_direction_driving": "🚗 规划驾车路线",
    "maps_direction_transit_integrated": "🚌 规划公交路线",
    "maps_direction_bicycling": "🚴 规划骑行路线",
    "maps_distance": "📏 计算距离",
}


def build_request(
    city: str,
    start_date: date,
    end_date: date,
    transport: list[str],
    hotel_type: str,
    preferences: list[str],
    extra: str,
) -> dict:
    """把表单参数转成 POST /api/v1/trips 的请求体（字段与 api.TripRequest 对齐）。"""
    return {
        "city": city.strip(),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "preferences": preferences,
        "hotel_type": hotel_type,
        "transport": transport,
        "extra": extra.strip(),
        "user_id": USER_ID,
    }


def plan_via_api(req: dict, status) -> tuple[dict | None, str | None]:
    """POST 提交规划 → 订阅 SSE 实时进度 → 返回 (plan, error)。

    status: st.status 容器，用于把工具进度逐条写出来。
    """
    try:
        # read 超时给足：规划可能 60-90s；SSE 每秒有 keepalive 注释帧，不会空闲超时。
        with httpx.Client(timeout=httpx.Timeout(15.0, read=180.0)) as client:
            resp = client.post(f"{API_BASE}/api/v1/trips", json=req)
            if resp.status_code == 429:
                return None, "请求过于频繁（已触发限流），请稍后再试。"
            resp.raise_for_status()
            task_id = resp.json()["task_id"]
            status.write(f"已提交，任务号 `{task_id}`，开始规划…")

            plan: dict | None = None
            error: str | None = None
            with client.stream("GET", f"{API_BASE}/api/v1/trips/{task_id}/stream") as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    # SSE：数据帧形如 "data: {json}"；": keepalive" 等注释帧忽略。
                    if not line or not line.startswith("data:"):
                        continue
                    try:
                        evt = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    etype = evt.get("type")
                    if etype == "tool_start":
                        name = evt.get("name") or ""
                        status.write(_TOOL_LABELS.get(name, f"🔧 {name}") + " …")
                    elif etype == "done":
                        plan = evt.get("plan")
                        break
                    elif etype == "error":
                        error = evt.get("message") or "规划失败"
                        break
            return plan, error
    except httpx.ConnectError:
        return None, (
            f"无法连接后端 API（{API_BASE}）。请确认 FastAPI 与 Celery worker 已启动"
            "（make dev / docker compose up）。"
        )
    except httpx.HTTPStatusError as exc:
        return None, f"API 返回错误：HTTP {exc.response.status_code}"
    except Exception as exc:  # noqa: BLE001
        return None, f"规划出错：{exc}"


def run_and_store(req: dict, label: str) -> None:
    """跑一次规划，成功则写入 session 并 rerun 渲染；失败则就地报错。"""
    st.session_state.last_request = req
    with st.status(label, expanded=True) as status:
        plan, error = plan_via_api(req, status)
        status.update(
            label="✅ 规划完成" if plan else "⚠️ 规划未完成",
            state="complete" if plan else "error",
        )
    if error:
        st.error(f"😟 {error}")
    elif not plan:
        st.error("😟 未获取到行程，请稍后重试或调整参数。")
    else:
        st.session_state.plan_data = plan
        st.rerun()


@st.cache_data(ttl=15, show_spinner=False)
def fetch_history(user_id: str) -> list[dict] | None:
    """拉取最近规划历史（缓存 15s，避免每次 rerun 都打 API 触发限流）。"""
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(
                f"{API_BASE}/api/v1/trips/history", params={"user_id": user_id}
            )
            resp.raise_for_status()
            return resp.json()
    except Exception:  # noqa: BLE001 —— 后端未起时静默降级
        return None


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
    transport_selected = [opt for opt in transport_options if st.checkbox(opt, key=f"trans_{opt}")]

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
    pref_selected = [opt for opt in pref_options if st.checkbox(opt, key=f"pref_{opt}")]

    st.markdown("---")
    st.markdown("### 💬 额外要求")
    extra_requirements = st.text_area(
        "补充说明",
        placeholder="例如: 带老人出行需要轻松行程、想在市中心活动...",
        label_visibility="collapsed",
    )

    st.markdown("---")
    submit_btn = st.button("🚀 开始规划", type="primary", use_container_width=True)

    # 历史记录（读自 Postgres，经 API；证明 UI 提交确实落库）。
    st.markdown("---")
    with st.expander("🕘 历史记录", expanded=False):
        history = fetch_history(USER_ID)
        if history is None:
            st.caption("（无法连接后端 API）")
        elif not history:
            st.caption("暂无记录")
        else:
            for r in history[:10]:
                icon = {"done": "✅", "error": "❌", "planning": "⏳", "pending": "⏳"}.get(r.get("status"), "•")
                st.caption(f"{icon} {r.get('city', '')} · {r.get('start_date', '')}~{r.get('end_date', '')}")


# ============ 会话状态 ============
if "plan_data" not in st.session_state:
    st.session_state.plan_data = None
if "last_request" not in st.session_state:
    st.session_state.last_request = None

# 引导页（尚无结果且未提交时）
if st.session_state.plan_data is None and not submit_btn:
    st.info("👈 在左侧填写旅行参数，然后点击 **开始规划** 按钮")
    col_a, col_b, col_c = st.columns(3)
    with col_a:
        st.markdown("##### 🌤️ 实时天气查询")
        st.caption("接入高德地图 MCP，获取目的地准确天气预报")
    with col_b:
        st.markdown("##### 🏛️ 智能景点推荐")
        st.caption("根据你的偏好，AI 精准匹配景点、酒店与路线")
    with col_c:
        st.markdown("##### 🗺️ 地图 + 预算")
        st.caption("行程地图可视化，门票/餐饮/住宿/交通费用一目了然")

# ============ 提交 → 调 API ============
if submit_btn:
    if not city.strip():
        st.error("请输入目的地城市")
    elif end_date < start_date:
        st.error("结束日期不能早于开始日期")
    else:
        req = build_request(
            city, start_date, end_date,
            transport_selected, hotel_type, pref_selected, extra_requirements,
        )
        run_and_store(req, "🤖 正在规划（天气 → 景点 → 酒店 → 路线 → 整合）...")


# ============ 结果展示 ============
plan = st.session_state.plan_data
if plan is not None:
    ui.render_plan_result(plan)

    # ---- 调整并重新规划（瘦客户端：带上修改意见重新发起一次完整规划）----
    st.markdown("---")
    st.markdown("### 🔄 调整并重新规划")
    st.caption("带上你的修改意见重新发起一次规划（会生成一条新的行程记录）。")
    modification = st.text_input(
        "修改要求",
        key="modify_input",
        placeholder="例如：多安排户外景点、酒店换到市中心、行程节奏放慢些",
    )
    if st.button("应用修改并重规划", use_container_width=True) and modification.strip():
        base = st.session_state.last_request
        if not base:
            st.error("没有可复用的上次请求，请直接在左侧重新规划。")
        else:
            new_req = dict(base)
            new_req["extra"] = f"{base.get('extra', '')} {modification.strip()}".strip()
            run_and_store(new_req, "🧩 正在按你的要求重新规划...")
