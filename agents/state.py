"""
LangGraph 共享状态定义 —— 整条行程规划流水线在节点之间传递的 TripState。
"""
from typing import TypedDict, Annotated
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
    # 出发地与往返时间（手动输入，无票务 API）：
    #   origin_city    —— 用户常驻/出发城市；用于生成往返交通段（空则不生成）
    #   arrival_time   —— 第一天预计抵达目的地的时间（HH:MM）；驱动首日「半天」截断
    #   departure_time —— 最后一天返程出发时间（HH:MM）；驱动末日「半天」截断
    origin_city: str
    arrival_time: str
    departure_time: str
    # 旅行人群画像：直接影响景点/酒店/餐厅与节奏的推荐逻辑。
    #   party_type  —— 同伴类型（独自一人 / 情侣出行 / 家庭亲子 / 朋友结伴 / 商务出行）
    #   party_size  —— 出行人数（0 表示未填）
    #   budget_level—— 预算档位（经济实惠 / 舒适适中 / 高端奢华）
    party_type: str
    party_size: int
    budget_level: str

    # Conversation history
    messages: Annotated[list, add_messages]

    # Intermediate results (populated by nodes)
    weather_data: dict | None
    poi_data: list | None
    hotel_data: list | None
    route_data: list | None
    # RAG 检索到的"内容证据"（攻略/口碑/玩法），best-effort：取不到则为 None。
    rag_context: str | None

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
