"""智能旅行助手 —— 独立 Streamlit 入口（魔搭 Studio 演示版）。

与 app.py 的区别：app.py 是「瘦客户端」，靠 httpx 调 FastAPI+Celery+Postgres 后端；
本入口**进程内直接跑 LangGraph 图**（复用 agents.planner.TripPlanner），
无需 FastAPI / Celery / Redis / Postgres，单进程即可运行，适合魔搭 Studio 免费部署。

依赖：仅需 DASHSCOPE_API_KEY（环境变量或 Studio secret）+ 出网访问高德 MCP。
用法：streamlit run app_studio.py
"""
import asyncio
import os

import streamlit as st

import ui  # 复用现有渲染器（render_plan_result）
from agents.planner import TripPlanner
from render import parse_plan

# 节点 → 进度标签（与 Agent.py 对齐）
NODE_LABELS = {
    "sanitize": "🧹 安全清洗输入…",
    "weather": "🌤️ 查询天气…",
    "poi": "🏛️ 搜索景点…",
    "hotel": "🏨 搜索酒店…",
    "route": "🚌 规划路线…",
    "rag": "📚 检索攻略知识（自我反思）…",
    "synthesize": "🧩 整合行程…",
    "geocode": "📍 补全坐标…",
    "optimize_route": "🗺️ 优化路线顺序…",
}

st.set_page_config(page_title="智能旅行助手", page_icon="🧳", layout="wide")


@st.cache_resource(show_spinner=False)
def get_planner() -> TripPlanner:
    """进程级单例：编译一次 LangGraph 图，跨会话复用。"""
    return TripPlanner()


def build_prompt(city, start_date, end_date, preferences, hotel_type, transport, extra) -> str:
    """把表单字段拼成 TripPlanner._parse_user_input 认识的自然语言格式。"""
    days = (end_date - start_date).days + 1
    parts = [f"{city}{days}日游，{start_date.isoformat()}-{end_date.isoformat()}"]
    if preferences:
        parts.append(f"喜欢{'、'.join(preferences)}")
    if hotel_type:
        parts.append(f"住宿偏好{hotel_type}")
    if transport:
        parts.append(f"交通方式偏好{'、'.join(transport)}")
    if extra.strip():
        parts.append(f"额外要求：{extra.strip()}")
    return "，".join(parts)


async def _run_stream(planner: TripPlanner, prompt: str, status) -> dict:
    """跑一次流式规划，实时把节点进度写到 status，返回最终 plan dict。"""
    final_plan: dict = {}
    seen: set[str] = set()
    async for event in planner.stream(prompt, thread_id=st.session_state.get("thread_id", "studio")):
        kind = event.get("event", "")
        name = event.get("name", "")
        if kind == "on_chain_start" and name in NODE_LABELS and name not in seen:
            seen.add(name)
            status.write(NODE_LABELS[name])
        if kind == "on_chain_end" and name == "synthesize":
            output = (event.get("data") or {}).get("output")
            if isinstance(output, dict) and output.get("final_plan"):
                final_plan = output["final_plan"]
    return final_plan


def plan_once(prompt: str) -> dict:
    """同步包装：新建事件循环跑一次流式规划（Streamlit 每次交互都是新执行）。"""
    planner = get_planner()
    with st.status("🚀 正在规划…", expanded=True) as status:
        try:
            plan = asyncio.run(_run_stream(planner, prompt, status))
            status.update(label="✅ 规划完成" if plan else "⚠️ 未生成有效行程",
                          state="complete" if plan else "error")
            return plan
        except Exception as exc:  # noqa: BLE001
            status.update(label=f"⚠️ 规划出错：{exc}", state="error")
            return {}


# ==================== 页面 ====================

st.title("🧳 智能旅行助手")
st.caption("Multi-Agent + Agentic RAG 行程规划演示 · 进程内直跑 LangGraph，无需后端服务")

# API Key 就绪检查：Studio 上通过 secret / 环境变量注入 DASHSCOPE_API_KEY。
if not os.environ.get("DASHSCOPE_API_KEY"):
    st.warning("⚠️ 未检测到 DASHSCOPE_API_KEY 环境变量。请在 Studio 的「设置 → 环境变量/Secret」中配置后重试。")

with st.sidebar:
    st.header("行程参数")
    from datetime import date, timedelta
    city = st.text_input("目的地城市", value="成都")
    col1, col2 = st.columns(2)
    with col1:
        start_date = st.date_input("出发日期", value=date.today() + timedelta(days=7))
    with col2:
        end_date = st.date_input("返回日期", value=date.today() + timedelta(days=9))
    preferences = st.multiselect(
        "偏好", ["美食", "自然风光", "历史文化", "亲子", "购物", "小众"], default=["美食", "历史文化"]
    )
    hotel_type = st.selectbox("住宿偏好", ["", "经济实惠", "舒适适中", "高端奢华"], index=2)
    transport = st.multiselect("交通方式", ["步行", "驾车", "公交", "骑行"], default=["步行", "公交"])
    extra = st.text_area("额外要求（选填）", placeholder="例如：不想太赶，想留时间逛夜市")
    go = st.button("🚀 开始规划", type="primary", use_container_width=True)

if go:
    if end_date < start_date:
        st.error("返回日期不能早于出发日期。")
    else:
        prompt = build_prompt(city, start_date, end_date, preferences, hotel_type, transport, extra)
        st.session_state["plan_data"] = plan_once(prompt)

# 渲染结果（复用现有 ui 渲染器；失败则回退到 JSON）
plan = st.session_state.get("plan_data")
if plan:
    try:
        # parse_plan 直接接受 dict；归一化后交给现有渲染器。
        ui.render_plan_result(parse_plan(plan))
    except Exception:  # noqa: BLE001 —— 渲染器签名不匹配时回退
        st.json(plan)
elif not go:
    st.info("👈 在左侧填写行程参数，点击「开始规划」。")
