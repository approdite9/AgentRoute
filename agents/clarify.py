"""规划前「主动追问」—— 据初步需求用 LLM 生成 3-4 个澄清问题。

设计要点：
  - 单次、非流式、结构化输出（ClarifyingQuestions schema），不调用任何 MCP 工具，
    因此很快（~5-15s）且不消耗高德配额；由 FastAPI 端点内联调用（无需 Celery）。
  - 结构化输出沿用 synthesis 的两条 ChatTongyi 经验：streaming=False + 精简提示。
  - 任意异常都向上抛给端点，由端点降级为「空问题列表」，绝不阻塞后续规划。
"""
from langchain_core.messages import SystemMessage, HumanMessage

from prompts import CLARIFY_PROMPT
from schemas import ClarifyingQuestions

# 已由表单显式收集、无需再问的字段（拼成给 LLM 的「已知信息」）。
_FIELD_LABELS = [
    ("city", "目的地"),
    ("start_date", "开始日期"),
    ("end_date", "结束日期"),
    ("preferences", "已选偏好"),
    ("hotel_type", "住宿偏好"),
    ("transport", "交通偏好"),
    ("extra", "额外要求"),
]


def _build_input(payload: dict) -> str:
    """把请求体描述成「已知信息」清单，供 LLM 据此避免重复提问。"""
    lines = ["【已知信息】"]
    for key, label in _FIELD_LABELS:
        val = payload.get(key)
        if isinstance(val, list):
            val = "、".join(val)
        if val:
            lines.append(f"{label}：{val}")
    lines.append("\n请据此提出 3-4 个澄清问题（避免重复以上已知信息）。")
    return "\n".join(lines)


async def generate_questions(payload: dict) -> list[dict]:
    """生成澄清问题列表；返回形如 [{id, question, kind, options}] 的 dict 列表。

    失败时由调用方兜底（端点 try/except → 返回空列表，UI 直接进入规划）。
    """
    from config import settings

    llm = settings.create_llm(streaming=False)
    # ChatTongyi 仅支持默认 function-calling；先试 json_mode，被拒则退回默认。
    try:
        structured = llm.with_structured_output(ClarifyingQuestions, method="json_mode")
    except (TypeError, ValueError):
        structured = llm.with_structured_output(ClarifyingQuestions)

    messages = [
        SystemMessage(content=CLARIFY_PROMPT),
        HumanMessage(content=_build_input(payload)),
    ]
    result: ClarifyingQuestions | None = await structured.ainvoke(messages)
    if not result or not result.questions:
        return []

    out: list[dict] = []
    for i, q in enumerate(result.questions[:4]):  # 最多 4 个
        text = (q.question or "").strip()
        if not text:
            continue
        # 开放题强制无选项；选择题至少要有 2 个选项，否则降级为开放题。
        kind = q.kind
        options = [o.strip() for o in (q.options or []) if o and o.strip()]
        if kind == "text":
            options = []
        elif len(options) < 2:
            kind, options = "text", []
        out.append({"id": f"q{i}", "question": text, "kind": kind, "options": options})
    return out
