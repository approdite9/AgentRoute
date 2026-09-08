"""
LangGraph StateGraph 组装 —— 把各节点连成完整的行程规划流水线。

流程（首轮规划 —— 并行 Fan-Out / Fan-In）：
    START ─(entry_router)→ ┬─ weather ─┐
                           ├─ poi ─────┼──→ route ─┬─(continue)→ review → rag → synthesize → geocode → optimize_route → END
                           └─ hotel ───┘           ├─(retry)───→ poi
                                                    └─(error)───→ error_handler → END

    weather / poi / hotel 三个采集节点**并行执行**（同一 LangGraph super-step），
    全部完成后汇聚到 route 节点（Fan-In）。route 依赖 poi_data 规划路线。
    rag 为 RAG 内容检索节点（best-effort）；geocode 补经纬度（best-effort）；
    optimize_route 做几何最近邻路线优化（best-effort）。三者失败都不影响出稿。

流程（多轮修改，复用同一 thread 的检查点）：
    START ─(entry_router)→ synthesize → geocode → optimize_route → END
    已有成稿 + 修改意见 → 直接重整合，跳过采集。

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
    rag_node,
    review_node,
    synthesis_node,
    geocode_node,
    optimize_route_node,
    error_node,
    is_transient_error,
)
from security import sanitize_trip_inputs


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


def entry_router(state: TripState) -> list[str]:
    """入口分流：

    - 多轮修改：检查点里已有成稿（final_plan）且本次带来了修改意见（user_feedback）
      → 直接进 synthesize 重整合，跳过 weather/poi/hotel/route 的重复采集（省 token）。
    - 其余（首轮规划）：Fan-Out 并行启动 weather + poi + hotel 三个采集节点。
      返回列表 → LangGraph 在同一 super-step 内并行执行所有目标节点。

    注意：interrupt 恢复走 Command(resume=...)，不经入口路由，故不会误判为修改。
    """
    if state.get("final_plan") and (state.get("user_feedback") or "").strip():
        return ["synthesize"]
    # Fan-Out: 三个采集节点并行执行（LangGraph 对条件边返回列表时并行调度所有目标）
    return ["weather", "poi", "hotel"]


def sanitize_node(state: TripState) -> dict:
    """入口安全节点：对用户可控字段执行 prompt injection 清洗 + 检测。

    在所有业务节点之前执行，确保进入 LLM 的数据已经过安全过滤。
    高风险注入尝试会被清空（字段置空）并记录审计日志。
    """
    sanitized = sanitize_trip_inputs(dict(state))
    # 只返回被修改的字段（LangGraph 会 merge 到 state 中）
    updates = {}
    for key in ["extra", "user_feedback", "city", "hotel_type", "origin_city", "preferences", "transport"]:
        if sanitized.get(key) != state.get(key):
            updates[key] = sanitized[key]
    return updates


def build_graph(checkpointer: Any = None) -> Any:
    builder = StateGraph(TripState)

    builder.add_node("sanitize", sanitize_node)
    builder.add_node("weather", weather_node)
    builder.add_node("poi", poi_node)
    builder.add_node("hotel", hotel_node)
    builder.add_node("route", route_node)
    builder.add_node("rag", rag_node)
    builder.add_node("review", review_node)
    builder.add_node("synthesize", synthesis_node)
    builder.add_node("geocode", geocode_node)
    builder.add_node("optimize_route", optimize_route_node)
    builder.add_node("error_handler", error_node)

    # START → sanitize: 所有用户输入先过安全清洗层（prompt injection 防护）
    builder.add_edge(START, "sanitize")

    # sanitize → entry_router: 清洗后分流到采集或修改路径
    builder.add_conditional_edges(
        "sanitize",
        entry_router,
        {"weather": "weather", "poi": "poi", "hotel": "hotel", "synthesize": "synthesize"},
    )

    # ── 并行采集 Fan-In ──────────────────────────────────────────────────
    # weather、poi、hotel 并行执行完毕后汇聚到 route 节点。
    # LangGraph 会等待 route 的所有入边（3 条）对应的节点全部完成后才执行 route。
    # route 依赖 poi_data 来规划路线；weather/hotel 数据为 best-effort 增补。
    builder.add_edge("weather", "route")
    builder.add_edge("poi", "route")
    builder.add_edge("hotel", "route")

    # 采集成功 → 人审断点 review → RAG 内容检索 → synthesize。失败仍按原逻辑重试 / 报错。
    builder.add_conditional_edges(
        "route",
        should_continue,
        {"retry": "poi", "continue": "review", "error": "error_handler"},
    )
    builder.add_edge("review", "rag")
    builder.add_edge("rag", "synthesize")
    # 整合后补坐标（best-effort）：给缺经纬度的景点/酒店用 maps_geo 补点，地图才能画。
    builder.add_edge("synthesize", "geocode")
    # 补完坐标后做几何最近邻路线优化（best-effort），再结束。
    builder.add_edge("geocode", "optimize_route")
    builder.add_edge("optimize_route", END)
    builder.add_edge("error_handler", END)

    checkpointer = checkpointer or MemorySaver()
    return builder.compile(checkpointer=checkpointer)


# Module-level compiled graph (shared across Streamlit sessions)
graph = build_graph()
