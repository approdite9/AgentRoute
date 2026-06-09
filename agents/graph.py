"""
LangGraph StateGraph 组装 —— 把各节点连成完整的行程规划流水线。

流程（首轮规划）：
    START ─(router)→ weather → poi → hotel → route ─┬─(continue)→ review → synthesize → END
                                                     ├─(retry)───→ poi
                                                     └─(error)───→ error_handler → END

流程（多轮修改，复用同一 thread 的检查点）：
    START ─(router)→ synthesize → END        # 已有成稿 + 修改意见 → 直接重整合，跳过采集

review 为人审断点：仅当 state["hitl_enabled"] 为真时 interrupt 暂停（交互式流程）；
否则透传，Celery / CLI 等自动化流程不受影响。
"""
from typing import Any

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

from agents.state import TripState
from agents.nodes import (
    weather_node,
    poi_node,
    hotel_node,
    route_node,
    review_node,
    synthesis_node,
    error_node,
    is_transient_error,
)


def should_continue(state: TripState) -> str:
    # error 只由 poi（唯一的 fatal 节点）写入——weather/hotel/route 为 best-effort，
    # 失败不写 error，故它们出错时这里仍判 continue，计划照常生成（降级而非失败）。
    error = state.get("error")
    if not error:
        return "continue"
    # 非瞬时错误（配额超限 / 参数非法 / Agent 死循环）重试也救不回来，且会再跑一遍
    # poi→hotel→route 白白消耗配额与 token —— 直接进入错误处理，不再重试。
    if not is_transient_error(error):
        return "error"
    if state.get("retry_count", 0) < 2:
        return "retry"
    return "error"


def entry_router(state: TripState) -> str:
    """入口分流：

    - 多轮修改：检查点里已有成稿（final_plan）且本次带来了修改意见（user_feedback）
      → 直接进 synthesize 重整合，跳过 weather/poi/hotel/route 的重复采集（省 token）。
    - 其余（首轮规划）：从 weather 开始正常跑全流程。

    注意：interrupt 恢复走 Command(resume=...)，不经入口路由，故不会误判为修改。
    """
    if state.get("final_plan") and (state.get("user_feedback") or "").strip():
        return "synthesize"
    return "weather"


def build_graph(checkpointer: Any = None) -> Any:
    builder = StateGraph(TripState)

    builder.add_node("weather", weather_node)
    builder.add_node("poi", poi_node)
    builder.add_node("hotel", hotel_node)
    builder.add_node("route", route_node)
    builder.add_node("review", review_node)
    builder.add_node("synthesize", synthesis_node)
    builder.add_node("error_handler", error_node)

    # Entry: 条件入口 —— 首轮走 weather，多轮修改直达 synthesize。
    builder.add_conditional_edges(
        START,
        entry_router,
        {"weather": "weather", "synthesize": "synthesize"},
    )
    builder.add_edge("weather", "poi")
    builder.add_edge("poi", "hotel")
    builder.add_edge("hotel", "route")
    # 采集成功 → 进人审断点 review；其后再 → synthesize。失败仍按原逻辑重试 / 报错。
    builder.add_conditional_edges(
        "route",
        should_continue,
        {"retry": "poi", "continue": "review", "error": "error_handler"},
    )
    builder.add_edge("review", "synthesize")
    builder.add_edge("synthesize", END)
    builder.add_edge("error_handler", END)

    checkpointer = checkpointer or MemorySaver()
    return builder.compile(checkpointer=checkpointer)


# Module-level compiled graph (shared across Streamlit sessions)
graph = build_graph()
