"""
行程规划总控 —— LangGraph StateGraph 的轻量封装。

TripPlanner 不再自己编排子 Agent，而是把请求解析成 TripState 后交给
预编译的 StateGraph（见 agents/graph.py）执行，对外仍暴露 invoke / stream。

用法：
    planner = TripPlanner()

    # 非流式（返回结构化行程 dict）
    plan = await planner.invoke("长沙3日游...", thread_id="abc")

    # 流式（逐个 yield LangGraph 事件）
    async for event in planner.stream("长沙3日游...", thread_id="abc"):
        ...
"""
import re
from contextlib import asynccontextmanager
from datetime import date
from typing import AsyncIterator

from langgraph.types import Command

from agents.state import TripState


# 默认偏好/交通字段的兜底值
_DEFAULT_PREFERENCES: list[str] = []
_DEFAULT_TRANSPORT: list[str] = []

# 日期匹配：兼容 "2026年5月21日" 与 "2026-05-21" / "2026/5/21"
_DATE_PATTERNS = [
    re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"),
    re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})"),
]

# 城市：取 "日游" 之前的部分，去掉中间的天数
_CITY_PATTERN = re.compile(r"^\s*(.+?)\s*\d*\s*日游")
# 各结构化字段（来自 app.build_prompt 的固定格式），均匹配到下一个分隔符为止
_SEP = r"[，,。；;\n]"
_PREF_PATTERN = re.compile(rf"喜欢\s*([^，,。；;\n]+)")
_HOTEL_PATTERN = re.compile(rf"住宿偏好\s*([^，,。；;\n]+)")
_TRANSPORT_PATTERN = re.compile(rf"交通方式偏好\s*([^，,。；;\n]+)")
_EXTRA_PATTERN = re.compile(rf"额外要求\s*[:：]\s*([^，,。；;\n]+)")

# 偏好/交通的分隔符（顿号、逗号、和、与、空格）
_LIST_SPLIT = re.compile(r"[、,，和与\s]+")


def _to_iso(year: str, month: str, day: str) -> str:
    """把零散的年月日转成 YYYY-MM-DD（非法日期则原样拼接兜底）。"""
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def _extract_dates(text: str) -> tuple[str, str]:
    """提取起止日期；只找到一个则起=止，找不到则返回空串。"""
    found: list[str] = []
    for pattern in _DATE_PATTERNS:
        for m in pattern.finditer(text):
            iso = _to_iso(*m.groups())
            if iso not in found:
                found.append(iso)
    if not found:
        return "", ""
    if len(found) == 1:
        return found[0], found[0]
    return found[0], found[1]


def _split_list(raw: str) -> list[str]:
    return [item for item in _LIST_SPLIT.split(raw.strip()) if item]


class TripPlanner:
    """StateGraph 的薄封装，负责解析自然语言输入并驱动流水线。"""

    def __init__(self):
        from agents.graph import graph
        self.graph = graph

    # ==================== 配置 ====================

    def _make_config(self, thread_id: str) -> dict:
        return {"configurable": {"thread_id": thread_id}}

    # ==================== 输入解析 ====================

    def _parse_user_input(self, text: str) -> TripState:
        """
        把自然语言需求解析为 TripState。

        识别：城市（"日游" 前）、起止日期、喜欢的偏好、住宿偏好、交通方式、额外要求。
        缺失字段使用合理默认值；原始文本同时写入 messages 与 extra 兜底。
        """
        text = text or ""

        city_m = _CITY_PATTERN.search(text)
        city = city_m.group(1).strip() if city_m else text.strip()[:10]

        start_date, end_date = _extract_dates(text)

        pref_m = _PREF_PATTERN.search(text)
        preferences = _split_list(pref_m.group(1)) if pref_m else list(_DEFAULT_PREFERENCES)

        hotel_m = _HOTEL_PATTERN.search(text)
        hotel_type = hotel_m.group(1).strip() if hotel_m else ""

        trans_m = _TRANSPORT_PATTERN.search(text)
        transport = _split_list(trans_m.group(1)) if trans_m else list(_DEFAULT_TRANSPORT)

        extra_m = _EXTRA_PATTERN.search(text)
        extra = extra_m.group(1).strip() if extra_m else ""

        return {
            "city": city,
            "start_date": start_date,
            "end_date": end_date,
            "preferences": preferences,
            "hotel_type": hotel_type,
            "transport": transport,
            "extra": extra,
            "messages": [{"role": "user", "content": text}],
            "weather_data": None,
            "poi_data": None,
            "hotel_data": None,
            "route_data": None,
            "final_plan": None,
            "user_feedback": None,
            "hitl_enabled": False,
            "error": None,
            "retry_count": 0,
        }

    # ==================== 非流式调用 ====================

    async def invoke(self, user_input: str, thread_id: str = "default") -> dict:
        """输入自然语言需求，返回结构化行程 dict（失败时为空 dict）。"""
        state = self._parse_user_input(user_input)
        result = await self.graph.ainvoke(state, config=self._make_config(thread_id))
        return result.get("final_plan") or {}

    # ==================== 流式调用 ====================

    async def stream(self, user_input: str, thread_id: str = "default") -> AsyncIterator[dict]:
        """逐个 yield LangGraph 的 astream_events(v2) 事件。"""
        state = self._parse_user_input(user_input)
        config = self._make_config(thread_id)
        async for event in self.graph.astream_events(state, config=config, version="v2"):
            yield event

    # ==================== 人审 + 多轮（Redis 检查点） ====================

    @asynccontextmanager
    async def _redis_graph(self):
        """按调用现场编译一张「带 Redis 检查点」的图，用完即释放连接。

        Streamlit 每次交互都 asyncio.run（全新事件循环），而 redis.asyncio 的连接
        绑定在创建它的 loop 上；若在模块级长持一个 saver，换 loop 复用会抛
        "got Future attached to a different loop"。因此这里用 from_conn_string
        上下文管理器「按调用」建池，跨调用（start_review → resume → modify）的状态
        全部经 Redis（按 thread_id）持久化衔接，天然支持多次 asyncio.run。

        检查点写入 redis db0，键前缀 checkpoint: / checkpoint_write:
        （可用 `redis-cli -n 0 keys 'checkpoint:*'` 观察）。
        """
        from langgraph.checkpoint.redis.aio import AsyncRedisSaver
        from agents.graph import build_graph
        from config import settings

        async with AsyncRedisSaver.from_conn_string(settings.redis_url) as saver:
            await saver.asetup()  # 幂等：首次创建 RediSearch 索引，之后是 no-op
            yield build_graph(saver)

    async def start_review(self, user_input: str, thread_id: str) -> dict:
        """启动一次带人审的规划：跑到 review 断点暂停，返回数据采集草稿摘要。

        返回:
          {"status": "review", "draft": {...}, "prompt": "..."}  —— 命中断点，draft 供前端预览
          {"status": "done",   "plan": {...}}                    —— 未触发断点（异常兜底）直接出稿
        """
        state = self._parse_user_input(user_input)
        state["hitl_enabled"] = True
        config = self._make_config(thread_id)
        async with self._redis_graph() as graph:
            result = await graph.ainvoke(state, config=config)
        interrupts = result.get("__interrupt__")
        if interrupts:
            # interrupt 负载形如 {"type","draft","prompt"}；拆出内层 draft 摘要给前端。
            payload = interrupts[0].value or {}
            return {
                "status": "review",
                "draft": payload.get("draft", {}),
                "prompt": payload.get("prompt", ""),
            }
        return {"status": "done", "plan": result.get("final_plan") or {}}

    async def resume(self, feedback: str, thread_id: str) -> dict:
        """用户确认 / 给出意见后，从 review 断点恢复，产出最终行程 dict。"""
        config = self._make_config(thread_id)
        async with self._redis_graph() as graph:
            result = await graph.ainvoke(Command(resume=feedback), config=config)
        return result.get("final_plan") or {}

    async def modify(self, modification: str, thread_id: str) -> dict:
        """多轮修改：复用同一 thread 的检查点，入口直达 synthesize 重整合（跳过采集）。"""
        config = self._make_config(thread_id)
        new_state = {
            "user_feedback": modification,
            "messages": [{"role": "user", "content": f"请修改行程：{modification}"}],
        }
        async with self._redis_graph() as graph:
            result = await graph.ainvoke(new_state, config=config)
        return result.get("final_plan") or {}
