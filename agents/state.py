"""
LangGraph 共享状态定义 —— 整条行程规划流水线在节点之间传递的 TripState。
"""
from typing import TypedDict, Annotated, Any
from langgraph.graph.message import add_messages


class TripState(TypedDict):
    # Input fields (set at graph start)
    city: str
    start_date: str
    end_date: str
    preferences: list[str]
    hotel_type: str
    transport: list[str]
    extra: str

    # Conversation history
    messages: Annotated[list, add_messages]

    # Intermediate results (populated by nodes)
    weather_data: dict | None
    poi_data: list | None
    hotel_data: list | None
    route_data: list | None

    # Final output
    final_plan: dict | None

    # Human-in-the-loop（人审断点 + 多轮修改）
    # user_feedback: review 断点恢复时用户填入的意见，或多轮修改的修改要求；
    #                为 None / 默认值时视为「直接生成」，不触发最小修改逻辑。
    # hitl_enabled: 是否启用人审断点。仅交互式 Streamlit 流程置 True；
    #               Celery / CLI 等自动化流程保持 False，review 节点直接透传不打断。
    user_feedback: str | None
    hitl_enabled: bool

    # Error tracking
    error: str | None
    retry_count: int
