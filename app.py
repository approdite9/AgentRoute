"""智能旅行助手 — Streamlit Web 界面（FastAPI 后端的瘦客户端）。

不再在进程内直接跑 LangGraph：提交参数后 POST 给 FastAPI（/api/v1/trips），
再订阅 SSE（/api/v1/trips/{task_id}/stream）实时展示规划进度，最终用
ui.render_plan_result 渲染。这样「UI 提交」与「API 提交」共用同一条
Celery → Postgres → Flower 链路——每次提交都会落库、可在历史/监控里看到。

后端地址由环境变量 API_BASE_URL 指定（本机默认 http://localhost:8000；
docker-compose 里覆盖为 http://api:8000）。
"""
import hmac
import json
import os
from datetime import date, time

import httpx
import streamlit as st

import ui

API_BASE = os.environ.get("API_BASE_URL", "http://localhost:8000").rstrip("/")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
def get_user_id() -> str:
    """用 demo token 作为 user_id，让每个用户只看到自己的历史记录。"""
    return st.session_state.get("demo_token") or "anonymous"


# ============================================================
# 演示准入：注册门控 + 配额检查
# ============================================================

def _demo_register(name: str, email: str, purpose: str) -> dict | None:
    try:
        resp = httpx.post(
            f"{API_BASE}/api/v1/demo/register",
            json={"name": name, "email": email, "purpose": purpose},
            timeout=10.0,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        st.error(f"注册失败，请稍后重试：{exc}")
        return None


def _demo_check(token: str) -> dict:
    try:
        resp = httpx.post(
            f"{API_BASE}/api/v1/demo/check",
            json={"token": token},
            timeout=10.0,
        )
        # 429 ≠ 后端故障：是 IP 限流（短时操作过多）。单独区分，给出准确提示，
        # 而不是误报「无法连接验证服务」。
        if resp.status_code == 429:
            return {"valid": False, "can_use": False, "reason": "rate_limited", "name": ""}
        resp.raise_for_status()
        return resp.json()
    except Exception:
        # 后端不通时拒绝（fail-closed），避免未经验证的用户绕过配额门控。
        return {"valid": False, "can_use": False, "reason": "backend_error", "name": ""}


def _demo_use(token: str) -> bool:
    try:
        resp = httpx.post(
            f"{API_BASE}/api/v1/demo/use",
            json={"token": token},
            timeout=10.0,
        )
        return resp.status_code == 200
    except Exception:
        return True  # 降级：不因记录失败而阻塞用户


def _admin_list_users() -> list[dict] | None:
    try:
        resp = httpx.get(
            f"{API_BASE}/admin/demo/users",
            headers={"x-admin-password": ADMIN_PASSWORD},
            timeout=10.0,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        st.error(f"获取用户列表失败：{exc}")
        return None


def _admin_approve(user_id: str, extra: int, notes: str) -> bool:
    try:
        resp = httpx.post(
            f"{API_BASE}/admin/demo/approve/{user_id}",
            json={"extra_quota": extra, "notes": notes},
            headers={"x-admin-password": ADMIN_PASSWORD},
            timeout=10.0,
        )
        return resp.status_code == 200
    except Exception:
        return False


def _admin_block(user_id: str) -> bool:
    try:
        resp = httpx.post(
            f"{API_BASE}/admin/demo/block/{user_id}",
            headers={"x-admin-password": ADMIN_PASSWORD},
            timeout=10.0,
        )
        return resp.status_code == 200
    except Exception:
        return False


def render_registration_gate() -> bool:
    """渲染注册/准入门控。返回 True 表示用户已通过验证、可以使用 Agent。"""
    params = st.query_params
    # Prefer session_state token (avoids writing token back to URL on reruns).
    # Fall back to URL param only on first load (e.g. direct link access).
    token = st.session_state.get("demo_token", "") or params.get("t", "")

    # --- 已有 token：检查配额 ---
    if token:
        # 本会话已验证过同一 token → 直接放行，**不再每次 rerun 都打 /demo/check**。
        # 否则用户每勾一个选项（Streamlit 都会 rerun）就调一次校验接口，几下就撞上
        # IP 限流（429）→ 误弹「无法连接验证服务」。配额仍准确：每次成功规划后会在
        # run_and_store 里清除该标记，强制下一次 rerun 重新校验，配额耗尽即拦截。
        if st.session_state.get("demo_validated") and st.session_state.get("demo_token") == token:
            return True
        info = _demo_check(token)
        reason = info.get("reason", "")
        name = info.get("name", "用户")
        if info.get("can_use"):
            # 把用户名存进 session，后续规划时一起写库
            st.session_state.setdefault("demo_user_name", info.get("name", ""))
            st.session_state["demo_token"] = token
            st.session_state["demo_validated"] = True
            st.session_state["quota_exhausted"] = False
            return True
        # 配额耗尽：**仍放行页面**，让用户能看到刚生成的行程；只标记配额耗尽，
        # 由 run_and_store 在「发起新规划」时拦截。避免一刀切 st.stop() 把结果也挡掉。
        if reason == "quota_exhausted":
            st.session_state["demo_token"] = token
            st.session_state.setdefault("demo_user_name", name)
            st.session_state["demo_validated"] = True       # 已确认有效，避免每次 rerun 重打校验
            st.session_state["quota_exhausted"] = True
            return True
        # 封禁 / 限流 / 后端故障：真正拦截整页（fail-closed）。
        if reason == "blocked":
            st.error(f"😔 {name}，你的账号已被封禁，如有疑问请联系管理员。")
            st.stop()
        if reason in ("rate_limited", "backend_error"):
            st.error(
                "⚠️ 操作过于频繁，请等几秒后刷新重试。" if reason == "rate_limited"
                else "⚠️ 无法连接验证服务，请稍后刷新重试。"
            )
            st.stop()
        # token 失效（token_invalid 等）：清掉坏 token，落到下方注册表单重新注册。
        st.session_state.pop("demo_token", None)
        st.session_state.pop("demo_validated", None)

    # --- 无 token：显示注册表单 ---
    st.markdown("## 👋 欢迎使用智能旅行助手（演示版）")
    st.info(
        "这是一个 AI 驱动的旅行规划助手演示。\n\n"
        "每位用户可免费体验 **1 次**完整行程规划，请填写下方信息后开始使用。"
    )

    with st.form("demo_register_form"):
        name = st.text_input("你的姓名 *", placeholder="例如：张三")
        email = st.text_input("联系邮箱 *", placeholder="your@email.com")
        purpose = st.text_area(
            "你打算规划什么旅行？（选填）",
            placeholder="例如：五一去杭州玩 3 天，想找亲子景点",
            height=80,
        )
        submitted = st.form_submit_button("🚀 开始免费体验", type="primary", use_container_width=True)

    if submitted:
        name = name.strip()
        email = email.strip()
        if not name:
            st.error("请填写姓名")
            return False
        if not email or "@" not in email:
            st.error("请填写有效的邮箱地址")
            return False

        with st.spinner("正在验证…"):
            result = _demo_register(name, email, purpose)

        if result:
            token = result["token"]
            st.session_state["demo_token"] = token
            st.session_state["demo_user_name"] = name
            if result.get("already_registered") and not result.get("can_use"):
                st.warning(
                    "该邮箱已注册且试用次数已用完。\n\n"
                    "如需继续使用请联系管理员：barrninerichard@gmail.com"
                )
                st.stop()
            st.rerun()

    return False


def render_admin_panel():
    """侧边栏管理面板（仅当 URL 含 ?admin=1 时显示入口）。"""
    if st.query_params.get("admin") != "1":
        return
    if not ADMIN_PASSWORD:
        return

    st.sidebar.markdown("---")
    st.sidebar.markdown("### 🔐 管理员面板")
    pwd = st.sidebar.text_input("管理密码", type="password", key="admin_pwd_input")
    if not hmac.compare_digest(pwd, ADMIN_PASSWORD):
        return

    st.sidebar.success("✅ 已验证")
    if st.sidebar.button("刷新用户列表", use_container_width=True):
        st.session_state["admin_users"] = _admin_list_users()

    users = st.session_state.get("admin_users") or _admin_list_users() or []
    st.session_state["admin_users"] = users

    st.sidebar.markdown(f"**共 {len(users)} 位注册用户**")

    for u in users:
        label = f"{'🚫' if u['is_blocked'] else ('✅' if u['remaining'] > 0 else '⏰')} {u['name']} ({u['email']})"
        with st.sidebar.expander(label, expanded=False):
            st.caption(f"用途：{u['purpose'] or '—'}")
            st.caption(f"配额：{u['used_count']} / {u['quota']}  剩余：{u['remaining']}")
            st.caption(f"注册：{(u['created_at'] or '')[:10]}  最后使用：{(u['last_used_at'] or '—')[:10]}")
            if u['admin_notes']:
                st.caption(f"备注：{u['admin_notes']}")

            col_a, col_b = st.columns(2)
            with col_a:
                notes = st.text_input("备注", key=f"notes_{u['id']}", placeholder="可选")
                if st.button("➕ +1 次", key=f"approve_{u['id']}", use_container_width=True):
                    if _admin_approve(u["id"], 1, notes):
                        st.success("已追加")
                        st.session_state["admin_users"] = _admin_list_users()
                        st.rerun()
            with col_b:
                st.markdown("&nbsp;", unsafe_allow_html=True)
                if not u["is_blocked"]:
                    if st.button("🚫 封禁", key=f"block_{u['id']}", use_container_width=True):
                        if _admin_block(u["id"]):
                            st.warning("已封禁")
                            st.session_state["admin_users"] = _admin_list_users()
                            st.rerun()

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
    party_type: str = "",
    party_size: int = 0,
    budget_level: str = "",
    origin_city: str = "",
    arrival_time: str = "",
    departure_time: str = "",
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
        "party_type": party_type,
        "party_size": party_size,
        "budget_level": budget_level,
        "origin_city": origin_city.strip(),
        "arrival_time": arrival_time,
        "departure_time": departure_time,
        "user_id": get_user_id(),
    }


def plan_via_api(req: dict, status) -> tuple[dict | None, str | None]:
    """POST 提交规划 → 订阅 SSE 实时进度 → 返回 (plan, error)。

    status: st.status 容器，用于把工具进度逐条写出来。
    """
    # 鉴权：写接口需带令牌（后端要求），统一用 x-demo-token 头。
    token = st.session_state.get("demo_token", "")
    auth_headers = {"x-demo-token": token} if token else {}
    try:
        # read 超时给足：规划可能 60-90s；SSE 每秒有 keepalive 注释帧，不会空闲超时。
        with httpx.Client(timeout=httpx.Timeout(15.0, read=180.0)) as client:
            resp = client.post(f"{API_BASE}/api/v1/trips", json=req, headers=auth_headers)
            if resp.status_code == 429:
                return None, "请求过于频繁（已触发限流），请稍后再试。"
            if resp.status_code in (401, 403):
                return None, "登录状态已失效，请刷新页面重新进入。"
            resp.raise_for_status()
            task_id = resp.json()["task_id"]
            status.write(f"已提交，任务号 `{task_id}`，开始规划…")

            plan: dict | None = None
            error: str | None = None
            stream_headers = auth_headers
            with client.stream("GET", f"{API_BASE}/api/v1/trips/{task_id}/stream", headers=stream_headers) as r:
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
    # 配额耗尽：拦截「发起新规划」（但不影响查看已生成的行程）。
    if st.session_state.get("quota_exhausted"):
        st.warning(
            "👋 你的免费试用次数已用完，无法发起新的规划；已生成的行程仍可查看。\n\n"
            "如需继续使用，请联系管理员审批：barrninerichard@gmail.com"
        )
        return
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
        # 规划成功：消耗一次演示配额，并清除会话验证缓存——
        # 下一次 rerun 会重新校验配额，用完即拦截（保证缓存不绕过配额）。
        token = st.session_state.get("demo_token", "")
        if token:
            _demo_use(token)
            st.session_state["demo_validated"] = False
        st.session_state.plan_data = plan
        st.session_state.phase = "input"  # 退出追问阶段，回到展示
        st.rerun()


def fetch_clarify(req: dict) -> list[dict]:
    """向 /trips/clarify 拉取澄清问题；失败/超时则返回空（直接进入规划）。"""
    token = st.session_state.get("demo_token", "")
    headers = {"x-demo-token": token} if token else {}
    try:
        with httpx.Client(timeout=httpx.Timeout(10.0, read=40.0)) as client:
            resp = client.post(f"{API_BASE}/api/v1/trips/clarify", json=req, headers=headers)
            resp.raise_for_status()
            return resp.json().get("questions") or []
    except Exception:  # noqa: BLE001 —— 追问失败不阻塞主流程
        return []


def fold_answers_into_request(base: dict, questions: list[dict]) -> dict:
    """把澄清问答拼进 extra，作为额外约束并入规划请求。"""
    parts: list[str] = []
    for q in questions:
        val = st.session_state.get(f"clarify_{q['id']}")
        if not val:
            continue
        answer = "、".join(val) if isinstance(val, list) else str(val).strip()
        if answer:
            parts.append(f"{q['question']} {answer}")
    new_req = dict(base)
    if parts:
        addition = "；".join(parts)
        new_req["extra"] = f"{base.get('extra', '')} 补充偏好：{addition}".strip()
    return new_req


@st.cache_data(ttl=15, show_spinner=False)
def fetch_history(user_id: str) -> list[dict] | None:
    """拉取最近规划历史（缓存 15s，避免每次 rerun 都打 API 触发限流）。"""
    if not user_id or user_id == "anonymous":
        return None
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(
                f"{API_BASE}/api/v1/trips/history",
                headers={"x-demo-token": user_id},
            )
            resp.raise_for_status()
            return resp.json()
    except Exception:  # noqa: BLE001 —— 后端未起时静默降级
        return None


def fetch_trip_plan(trip_id: str) -> dict | None:
    """拉取某条历史行程的完整计划（plan_json）。配额用完也能回看自己的行程。"""
    token = get_user_id()
    if not token or token == "anonymous":
        return None
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(
                f"{API_BASE}/api/v1/trips/history/{trip_id}",
                headers={"x-demo-token": token},
            )
            resp.raise_for_status()
            return resp.json().get("plan")
    except Exception:  # noqa: BLE001
        return None


# ============================================================
# 门控：未通过则停止渲染，通过后继续展示规划 UI
# ============================================================
if not render_registration_gate():
    st.stop()

render_admin_panel()

# ---- 主 UI ----
st.markdown('<div class="main-header">🧳 智能旅行助手</div>', unsafe_allow_html=True)

# 配额耗尽提示：放在顶部、不打断结果展示——用户既能看到已生成的行程，也明白为何无法再发起。
if st.session_state.get("quota_exhausted"):
    st.info(
        "ℹ️ 你的免费试用次数已用完，**已生成的行程仍可在下方查看与导出**；"
        "如需继续规划，请联系管理员审批：barrninerichard@gmail.com"
    )

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
    st.markdown("### ✈️ 出发地与往返")
    origin_city = st.text_input(
        "📍 出发城市",
        placeholder="例如: 上海（填了才生成往返交通与半天逻辑）",
    )
    know_times = st.checkbox(
        "✅ 我已知道往返时间",
        value=False,
        help="勾选后填写抵达/返程时间；行程会据此安排首尾日的「半天」，更贴近真实体验。",
    )
    arrival_time = departure_time = ""
    if know_times:
        ac1, ac2 = st.columns(2)
        with ac1:
            at = st.time_input("🛬 抵达时间", value=time(14, 0), step=1800)
            arrival_time = at.strftime("%H:%M")
        with ac2:
            dt = st.time_input("🛫 返程出发", value=time(11, 0), step=1800)
            departure_time = dt.strftime("%H:%M")

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
    st.markdown("### 👥 旅行人群")
    party_type = st.selectbox(
        "同伴类型",
        ["不限", "独自一人", "情侣出行", "家庭亲子", "朋友结伴", "商务出行"],
        index=0,
    )
    if party_type == "不限":
        party_type = ""
    party_size = st.number_input("出行人数", min_value=0, max_value=30, value=0, step=1,
                                 help="0 表示不指定；会影响餐饮/门票费用估算。")
    budget_level = st.select_slider(
        "预算档位",
        options=["不限", "经济实惠", "舒适适中", "高端奢华"],
        value="不限",
    )
    if budget_level == "不限":
        budget_level = ""

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
    clarify_on = st.checkbox(
        "🤔 规划前智能追问",
        value=True,
        help="规划前先问你 3-4 个问题，把需求问清楚再定制行程。",
    )
    submit_btn = st.button("🚀 开始规划", type="primary", use_container_width=True)

    # 历史记录（读自 Postgres，经 API）：完成的行程可点「查看」回看完整计划，
    # 配额用完也能查看（详情接口与配额无关，仅校验本人 token）。
    st.markdown("---")
    with st.expander("🕘 历史记录", expanded=False):
        history = fetch_history(get_user_id())
        if history is None:
            st.caption("（无法连接后端 API）")
        elif not history:
            st.caption("暂无记录")
        else:
            st.caption("点击已完成（✅）的行程即可回看完整计划")
            for r in history[:10]:
                status = r.get("status")
                icon = {"done": "✅", "error": "❌", "planning": "⏳", "pending": "⏳"}.get(status, "•")
                label = f"{icon} {r.get('city', '')} · {r.get('start_date', '')}~{r.get('end_date', '')}"
                if status == "done":
                    # 整行做成按钮：窄侧边栏里比 columns + 小按钮更稳、更好点。
                    if st.button(label, key=f"view_{r['id']}", use_container_width=True):
                        with st.spinner("加载行程…"):
                            past = fetch_trip_plan(r["id"])
                        if past:
                            st.session_state.plan_data = past
                            st.session_state.phase = "input"
                            st.rerun()
                        else:
                            st.warning("无法加载该行程，请稍后重试。")
                else:
                    st.caption(label)


# ============ 会话状态 ============
if "plan_data" not in st.session_state:
    st.session_state.plan_data = None
if "last_request" not in st.session_state:
    st.session_state.last_request = None
if "phase" not in st.session_state:                 # input → clarify（→ 展示）
    st.session_state.phase = "input"
if "clarify_questions" not in st.session_state:
    st.session_state.clarify_questions = []
if "base_request" not in st.session_state:
    st.session_state.base_request = None

# 引导页（尚无结果、未提交、且不在追问阶段时）
if (
    st.session_state.plan_data is None
    and st.session_state.phase == "input"
    and not submit_btn
):
    st.info("👈 在左侧填写旅行参数，然后点击 **开始规划** 按钮")
    col_a, col_b, col_c = st.columns(3)
    with col_a:
        st.markdown("##### 🤔 规划前智能追问")
        st.caption("先问清你的同伴、节奏、口味，再据此定制行程")
    with col_b:
        st.markdown("##### 🏛️ 智能景点推荐")
        st.caption("根据你的偏好，AI 精准匹配景点、酒店与路线")
    with col_c:
        st.markdown("##### 🗺️ 地图 + 预算")
        st.caption("行程地图可视化，门票/餐饮/住宿/交通费用一目了然")

# ============ 提交 → （可选追问）→ 调 API ============
if submit_btn:
    if not city.strip():
        st.error("请输入目的地城市")
    elif end_date < start_date:
        st.error("结束日期不能早于开始日期")
    else:
        req = build_request(
            city, start_date, end_date,
            transport_selected, hotel_type, pref_selected, extra_requirements,
            party_type, int(party_size), budget_level,
            origin_city, arrival_time, departure_time,
        )
        if clarify_on:
            with st.spinner("🤔 正在想要问你哪些问题…"):
                questions = fetch_clarify(req)
            if questions:
                st.session_state.base_request = req
                st.session_state.clarify_questions = questions
                st.session_state.phase = "clarify"
                st.rerun()
            else:
                # 追问失败 / 无问题 → 直接规划，不阻塞。
                run_and_store(req, "🤖 正在规划（天气 → 景点 → 酒店 → 路线 → 整合）...")
        else:
            run_and_store(req, "🤖 正在规划（天气 → 景点 → 酒店 → 路线 → 整合）...")


# ============ 追问阶段：回答澄清问题 → 折叠进 extra → 规划 ============
if st.session_state.phase == "clarify" and st.session_state.clarify_questions:
    questions = st.session_state.clarify_questions
    base = st.session_state.base_request or {}
    st.markdown(
        f'<div class="plan-title">🤔 几个小问题，帮你把 {base.get("city", "")} 行程定制得更贴心</div>',
        unsafe_allow_html=True,
    )
    st.caption("回答后点「确认并生成」；也可以直接跳过。")

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
        if st.button("✅ 确认并生成计划", type="primary", use_container_width=True):
            new_req = fold_answers_into_request(base, questions)
            run_and_store(new_req, "🧩 正在按你的回答定制行程…")
    with col_skip:
        if st.button("⏭️ 跳过", use_container_width=True):
            run_and_store(base, "🤖 正在规划（天气 → 景点 → 酒店 → 路线 → 整合）...")


# ============ 结果展示 ============
plan = st.session_state.plan_data
if plan is not None:
    ui.render_plan_result(plan)

    # ---- 调整并微调（多轮修改：带上原计划 + 修改意见，直达整合做「最小必要修改」）----
    st.markdown("---")
    st.markdown("### 🔄 调整并微调")
    st.caption(
        "在当前行程上做局部调整：仅改动你指出的部分，其余尽量保留。"
        "比直接重规划更快、更省（跳过重复的天气/景点/酒店/路线采集）。"
    )
    modification = st.text_input(
        "修改要求",
        key="modify_input",
        placeholder="例如：第2天换成室内景点、酒店换到市中心、行程节奏放慢些",
    )
    if st.button("应用修改", type="primary", use_container_width=True) and modification.strip():
        base = st.session_state.last_request
        if not base:
            st.error("没有可复用的上次请求，请直接在左侧重新规划。")
        else:
            # 带上一版成稿 + 修改要求：后端据此走 entry_router → synthesize 的最小修改路径。
            new_req = dict(base)
            new_req["prev_plan"] = plan
            new_req["feedback"] = modification.strip()
            run_and_store(new_req, "🧩 正在按你的要求微调当前行程…")
