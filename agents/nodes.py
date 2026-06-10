"""
LangGraph 节点函数 —— 行程规划流水线的各个工作单元。

每个节点：
  1. 现场创建 LLM（不在模块级缓存），按领域取 MCP 工具
  2. 用 try/except 包裹主调用，异常时写入 state["error"] 并返回
  3. 用 structlog 记录开始/结束/错误
  4. 用 tenacity @retry 包裹真正的 LLM / MCP 调用

约定（用于配合 graph.should_continue 的重试/错误分流）：
  - 成功：返回数据并把 error 清空（{"<key>": result, "error": None}）
  - 失败：返回 {"<key>": None, "error": "<node>: <msg>", "retry_count": +1}
    retry_count 单调递增，确保 route 处的条件边最终一定能跳出重试循环。
"""
import json
import re
import time
from typing import Any

import structlog
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential
from langchain_core.messages import SystemMessage, HumanMessage
from langgraph.types import interrupt

from agents.state import TripState
from agents.specialist import SpecialistAgent
from mcp_client import McpClientManager
from cache.client import cached, last_cache_hit, TTL_WEATHER, TTL_POI
from monitoring.metrics import NODE_DURATION
from render import parse_plan
from prompts import (
    WEATHER_AGENT_PROMPT,
    ATTRACTION_AGENT_PROMPT,
    HOTEL_AGENT_PROMPT,
    ROUTE_AGENT_PROMPT,
    SYNTHESIS_AGENT_PROMPT,
    SYNTHESIS_STRUCTURED_PROMPT,
)

logger = structlog.get_logger(__name__)

# review 断点恢复 / 多轮修改的「空意见」哨兵：用户留空时填入此默认值，
# 表示「无修改、直接生成」——synthesis 据此跳过最小修改逻辑，走常规整合。
DEFAULT_RESUME = "请继续生成完整计划"

# 非瞬时错误：重试既无意义又会成倍消耗每日配额 / token，必须「快速失败」。
#   - 高德配额类：USER_DAILY_QUERY_OVER_LIMIT / QUOTA / DAILY
#   - 参数 / 鉴权类：INVALID_PARAMS / MISSING_REQUIRED_PARAMS / INVALID_KEY
#   - Agent 死循环触顶：GraphRecursionError（消息含 RECURSION）
# MISSING_REQUIRED_PARAMS：如骑行/路线工具缺 origin/destination —— 重试同样的坏参数
# 只会重跑整段 ReAct（成倍调用），故归为永久错误、一次即止。
_NON_RETRYABLE_TOKENS = (
    "OVER_LIMIT",
    "QUOTA",
    "DAILY_QUERY",
    "INVALID_PARAMS",
    "MISSING_REQUIRED_PARAMS",
    "REQUIRED_PARAMS",
    "INVALID_KEY",
    "INVALID_USER_KEY",
    "RECURSION",
    # 模型生成了非法的 tool_call 参数（DashScope 400）——重试只会重复同样的坏调用。
    "INVALIDPARAMETER",
    "MUST BE IN JSON",
)


def is_transient_error(message: str) -> bool:
    """按错误文本判断是否为瞬时错误（可重试）；配额/参数/死循环等视为永久错误。"""
    msg = (message or "").upper()
    return not any(token in msg for token in _NON_RETRYABLE_TOKENS)


def _is_transient(exc: BaseException) -> bool:
    """仅对疑似瞬时错误（网络抖动 / 限流 / 超时）重试；配额/参数/死循环等直接放弃。"""
    return is_transient_error(str(exc))


# 统一的重试策略：仅瞬时错误最多重试 3 次、指数退避；非瞬时错误一次即失败，
# 避免一个配额错误被 tenacity 放大成 3 次 Agent 调用（再叠加 graph 重试边）。
_RETRY_KWARGS = dict(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception(_is_transient),
)


def _failure(node: str, key: str, state: TripState, exc: Exception) -> dict:
    """统一构造失败返回值：清空数据键、记录错误、递增 retry_count。"""
    logger.error("node_error", node=node, error=str(exc))
    return {
        key: None,
        "error": f"{node}: {exc}",
        "retry_count": state.get("retry_count", 0) + 1,
    }


async def _invoke_specialist(
    *,
    domain: str,
    agent_name: str,
    prompt: str,
    query: str,
) -> str:
    """真正的「贵」调用：现场建 LLM + 按领域取 MCP 工具 + 运行子 Agent（带重试）。

    抽成独立函数，便于 weather/poi 在节点层用 @cached 装饰复用其结果。
    异常向上抛出，由调用方（节点）统一转成可分流的 error。
    """
    # 1. 现场创建 LLM（不在模块级缓存）+ 按领域取 MCP 工具
    from config import settings

    # 子 Agent 用**非流式** LLM：流式下 ChatTongyi 的 tool_call args 由增量拼装，
    # 偶发组装成非法 JSON → DashScope 400「function.arguments must be in JSON format」，
    # 并被 ReAct/tenacity 放大成反复调用。非流式让工具参数一次成型，更稳（与 synthesize 一致）。
    llm = settings.create_llm(streaming=False)
    tools = await McpClientManager().get_tools_for(domain)
    agent = SpecialistAgent(llm, agent_name, prompt, tools)

    # 4. tenacity 包裹真正的子 Agent 调用
    @retry(**_RETRY_KWARGS)
    async def _invoke() -> Any:
        return await agent.invoke(query)

    return await _invoke()


async def _run_specialist(
    state: TripState,
    *,
    node: str,
    domain: str,
    agent_name: str,
    prompt: str,
    query: str,
    data_key: str,
    fatal: bool = True,
) -> dict:
    """weather / poi / hotel / route 子 Agent 节点的公共执行逻辑（不带缓存）。

    fatal=True（默认，仅 poi 用）：失败写入 error、递增 retry_count，触发图的重试/报错。
    fatal=False（weather/hotel/route 等「增补」节点）：**最佳努力**——失败只记日志、
      返回 data=None，且**不触碰 error**，从而不让一个非关键节点（如路线工具缺参）
      搞垮整张计划。整条流水线的「闸门」只由 poi 把守（无景点才算真失败）。
    """
    logger.info("node_start", node=node, city=state.get("city"))
    t0 = time.perf_counter()
    try:
        result = await _invoke_specialist(
            domain=domain, agent_name=agent_name, prompt=prompt, query=query
        )
        elapsed = time.perf_counter() - t0
        NODE_DURATION.labels(node=node).observe(elapsed)
        logger.info(
            "node_done", node=node, duration_ms=int(elapsed * 1000), cache_hit=False
        )
        # 成功：fatal 节点清空 error 作为闸门信号；best-effort 节点不触碰 error。
        return {data_key: result, "error": None} if fatal else {data_key: result}
    except Exception as exc:  # noqa: BLE001 —— 节点内吞掉异常
        NODE_DURATION.labels(node=node).observe(time.perf_counter() - t0)
        if fatal:
            return _failure(node, data_key, state, exc)
        # best-effort：降级而非失败——仅记日志，data 置空，不写 error / 不增 retry_count。
        logger.warning("node_soft_fail", node=node, error=str(exc))
        return {data_key: None}


# ==================== 数据采集节点 ====================

# 缓存粒度 = 业务键（城市 + 日期 / 城市 + 偏好类别）。@cached 用函数参数名
# 填充 key 模板，因此这两个 _fetch_* 的参数名必须与模板占位符一致。
# 返回 dict（而非裸字符串）：cache 层统一以 JSON dict 存取，也便于日后扩展字段。

def _reject_empty(content: str, what: str) -> str:
    """子 Agent 返回空/空白即视为失败并抛错——关键是**不要把空结果写进缓存**。

    此前一旦工具失败但 ReAct 优雅返回空文本，`{"content": ""}` 会被缓存 TTL_POI=24h，
    导致后续同城同偏好全部命中空缓存：景点全靠 LLM 编造、无坐标 → 地图画不出来。
    抛错后 @cached 不缓存，poi（fatal）据此重试/报错，weather（best-effort）软失败。
    """
    if not content or not content.strip():
        raise ValueError(f"{what} 返回空结果（不写入缓存）")
    return content


@cached("weather:{city}:{date}", TTL_WEATHER)
async def _fetch_weather(city: str, date: str) -> dict:
    """查询天气（实际经子 Agent 调用高德 maps_weather MCP）。结果按 city+date 缓存。"""
    query = f"请查询「{city}」在 {date} 期间的天气情况。"
    content = await _invoke_specialist(
        domain="weather",
        agent_name="WeatherAgent",
        prompt=WEATHER_AGENT_PROMPT,
        query=query,
    )
    return {"content": _reject_empty(content, "天气查询")}


@cached("poi:{city}:{category}", TTL_POI)
async def _fetch_poi(city: str, category: str) -> dict:
    """按偏好搜索景点（经子 Agent 调用高德 POI MCP）。结果按 city+category 缓存。"""
    query = f"请在「{city}」搜索符合以下偏好的景点：{category}。"
    content = await _invoke_specialist(
        domain="poi",
        agent_name="AttractionAgent",
        prompt=ATTRACTION_AGENT_PROMPT,
        query=query,
    )
    return {"content": _reject_empty(content, "景点搜索")}


async def weather_node(state: TripState) -> dict:
    """查询目的地天气（命中缓存时跳过 MCP 调用）。"""
    city = state["city"]
    # 同一城市 + 同一日期区间 → 命中同一缓存键。
    date = f"{state.get('start_date', '')}~{state.get('end_date', '')}"
    logger.info("node_start", node="weather", city=city)
    t0 = time.perf_counter()
    try:
        fetched = await _fetch_weather(city, date)
        elapsed = time.perf_counter() - t0
        NODE_DURATION.labels(node="weather").observe(elapsed)
        logger.info(
            "node_done",
            node="weather",
            duration_ms=int(elapsed * 1000),
            cache_hit=last_cache_hit(),
        )
        # best-effort：天气是增补项，不触碰 error（闸门交给 poi）。
        return {"weather_data": fetched.get("content")}
    except Exception as exc:  # noqa: BLE001
        NODE_DURATION.labels(node="weather").observe(time.perf_counter() - t0)
        logger.warning("node_soft_fail", node="weather", error=str(exc))
        return {"weather_data": None}


async def poi_node(state: TripState) -> dict:
    """按用户偏好搜索景点（命中缓存时跳过 MCP 调用）。"""
    city = state["city"]
    category = "、".join(state.get("preferences") or []) or "综合各类热门"
    logger.info("node_start", node="poi", city=city)
    t0 = time.perf_counter()
    try:
        fetched = await _fetch_poi(city, category)
        elapsed = time.perf_counter() - t0
        NODE_DURATION.labels(node="poi").observe(elapsed)
        logger.info(
            "node_done",
            node="poi",
            duration_ms=int(elapsed * 1000),
            cache_hit=last_cache_hit(),
        )
        return {"poi_data": fetched.get("content"), "error": None}
    except Exception as exc:  # noqa: BLE001
        NODE_DURATION.labels(node="poi").observe(time.perf_counter() - t0)
        return _failure("poi", "poi_data", state, exc)


async def hotel_node(state: TripState) -> dict:
    """按住宿偏好搜索酒店。"""
    hotel_type = state.get("hotel_type") or "不限类型"
    query = f"请在「{state['city']}」搜索「{hotel_type}」的酒店。"
    return await _run_specialist(
        state,
        node="hotel",
        domain="hotel",
        agent_name="HotelAgent",
        prompt=HOTEL_AGENT_PROMPT,
        query=query,
        data_key="hotel_data",
        fatal=False,  # 酒店是增补项：搜不到也不该让整张计划失败
    )


async def route_node(state: TripState) -> dict:
    """根据交通偏好和已搜到的景点，规划景点间路线（调用 MCP 路线工具）。"""
    transport = "、".join(state.get("transport") or []) or "不限交通方式"
    poi = state.get("poi_data") or "（暂无景点数据，请基于城市常识规划主要景点间路线）"
    query = (
        f"城市：{state['city']}。出行交通偏好：{transport}。\n"
        f"以下是候选景点信息：\n{poi}\n"
        f"请据此规划主要景点之间的交通路线。"
    )
    return await _run_specialist(
        state,
        node="route",
        domain="route",
        agent_name="RouteAgent",
        prompt=ROUTE_AGENT_PROMPT,
        query=query,
        data_key="route_data",
        fatal=False,  # 路线是增补项：工具缺参/失败（如骑行缺坐标）也不该搞垮整张计划
    )


async def rag_node(state: TripState) -> dict:
    """RAG 检索：按城市+偏好检索旅行知识（攻略/口碑/玩法）作为整合的"内容证据"。

    best-effort：检索失败 / 该城市无语料 / 无命中 都不报错（rag_context=None），
    绝不影响计划生成。检索在线程池执行，避免（DashScope embedding 时）阻塞事件循环。
    """
    import asyncio

    city = state.get("city", "")
    prefs = "、".join(state.get("preferences") or []) or "热门 玩法 美食 必去"
    logger.info("node_start", node="rag", city=city)
    t0 = time.perf_counter()
    try:
        from rag.pipeline import get_default_pipeline, format_context

        pipe = get_default_pipeline()
        # 该城市有语料则按城市做结构化过滤；否则全库语义召回兜底。
        has_city = any(c.meta.get("city") == city for c in pipe.chunks.values())
        where = {"city": city} if has_city else None
        query = f"{city} {prefs} 攻略 玩法 美食 避坑"
        hits = await asyncio.to_thread(pipe.retrieve, query, where, 4)
        ctx = format_context(hits)
        NODE_DURATION.labels(node="rag").observe(time.perf_counter() - t0)
        logger.info(
            "node_done", node="rag",
            duration_ms=int((time.perf_counter() - t0) * 1000), chunks=len(hits),
        )
        return {"rag_context": ctx or None}
    except Exception as exc:  # noqa: BLE001 —— best-effort，不触碰 error
        NODE_DURATION.labels(node="rag").observe(time.perf_counter() - t0)
        logger.warning("node_soft_fail", node="rag", error=str(exc))
        return {"rag_context": None}


# ==================== 人审断点节点 ====================

def _preview(val: Any, limit: int = 180) -> dict:
    """把某类采集数据压成 UI 友好的预览：是否就绪 + 字符数 + 截断片段。

    本项目里 weather/poi/hotel/route 数据均为子 Agent 的文本输出（str），
    因此不按「条数」统计（len(str) 会得到字符数而非条数、产生误导），
    而是统一给出「是否已采集 + 规模 + 片段」，供前端预览展示。
    """
    if not val:
        return {"ready": False, "chars": 0, "preview": ""}
    text = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
    snippet = " ".join(text.split())
    if len(snippet) > limit:
        snippet = snippet[:limit] + "…"
    return {"ready": True, "chars": len(text), "preview": snippet}


def review_node(state: TripState) -> dict:
    """人审断点：数据采集完成后暂停，把草稿摘要交回调用方收集反馈。

    仅当 state["hitl_enabled"] 为真时触发 interrupt（交互式 Streamlit 流程）；
    Celery / CLI 等自动化流程不带该标志，本节点直接透传、不打断流水线。

    interrupt() 暂停后，调用方用 Command(resume=<feedback>) 恢复执行：届时
    interrupt() 的返回值即该 feedback，写入 state["user_feedback"] 供整合节点使用。
    恢复时本节点会从头重跑，因此保持轻量、幂等（只读 state、不做副作用）。
    """
    if not state.get("hitl_enabled"):
        return {}

    draft_summary = {
        "city": state.get("city"),
        "start_date": state.get("start_date"),
        "end_date": state.get("end_date"),
        "weather": _preview(state.get("weather_data")),
        "poi": _preview(state.get("poi_data")),
        "hotel": _preview(state.get("hotel_data")),
        "route": _preview(state.get("route_data")),
    }
    logger.info("review_interrupt", city=state.get("city"))
    feedback = interrupt(
        {
            "type": "plan_review",
            "draft": draft_summary,
            "prompt": "数据收集完成。请确认继续生成完整计划，或输入修改意见。",
        }
    )
    return {
        "user_feedback": feedback,
        "messages": [{"role": "user", "content": feedback}],
    }


# ==================== 综合 / 错误节点 ====================

def _build_synthesis_input(state: TripState) -> str:
    """把已采集的全部数据 + 行程参数拼成给整合 LLM 的输入。"""
    parts = [
        "【行程参数】",
        f"城市：{state.get('city', '')}",
        f"日期：{state.get('start_date', '')} 至 {state.get('end_date', '')}",
        f"旅行偏好：{'、'.join(state.get('preferences') or []) or '无'}",
        f"住宿偏好：{state.get('hotel_type') or '不限'}",
        f"交通偏好：{'、'.join(state.get('transport') or []) or '不限'}",
        f"额外要求：{state.get('extra') or '无'}",
        "",
        "【天气数据】",
        str(state.get("weather_data") or "（无）"),
        "",
        "【景点数据】",
        str(state.get("poi_data") or "（无）"),
        "",
        "【酒店数据】",
        str(state.get("hotel_data") or "（无）"),
        "",
        "【路线数据】",
        str(state.get("route_data") or "（无）"),
        "",
        "【内容参考（攻略/口碑/玩法，每条含[出处]）】",
        str(state.get("rag_context") or "（无）"),
        "",
        "请依据以上真实数据整合出完整旅行计划，严格按系统提示中的 JSON 格式，只输出 JSON。",
        "可参考【内容参考】丰富景点描述、玩法与避坑建议，使行程更具体可执行；",
        "但务必以采集到的真实数据为准，不要编造【内容参考】之外的事实。",
    ]
    return "\n".join(parts)


def _apply_feedback(user_input: str, state: TripState) -> str:
    """按 user_feedback 增补整合输入：

    - 多轮修改（已有 final_plan + 有效意见）：带上原计划，引导「最小必要修改」，
      仅改动相关部分。配合 graph 入口把这类请求直接路由到 synthesize、跳过
      weather/poi/hotel/route 各子 Agent，从而省下重复采集的 token。
    - 首轮人审反馈（无 final_plan + 有效意见）：把用户在数据预览后补充的意见
      作为额外约束并入本次整合。
    - 无有效意见（None / 默认哨兵）：原样返回，走常规整合。
    """
    feedback = (state.get("user_feedback") or "").strip()
    if not feedback or feedback == DEFAULT_RESUME:
        return user_input

    original = state.get("final_plan")
    if original:
        return (
            user_input
            + "\n\n【原始计划 JSON】\n"
            + json.dumps(original, ensure_ascii=False)
            + f"\n\n【用户修改要求】{feedback}\n"
            "请在原始计划基础上做**最小必要修改**：仅调整与修改要求直接相关的部分，"
            "其余内容尽量原样保留，并返回完整的旅行计划。"
        )
    return (
        user_input
        + f"\n\n【用户在数据预览后补充的修改意见】{feedback}\n"
        "请在整合行程时优先满足上述意见。"
    )


async def synthesis_node(state: TripState) -> dict:
    """调用整合 LLM，用 Pydantic v2 结构化输出整合成最终行程 JSON。"""
    logger.info("node_start", node="synthesize", city=state.get("city"))
    t0 = time.perf_counter()
    try:
        from config import settings
        from schemas import TravelPlan

        # 结构化输出必须非流式：流式下 ChatTongyi 的 tool_call args 会被拆散、
        #   组装不全（缺 end_date / days 等），导致 Pydantic 校验失败、退回容错解析。
        llm = settings.create_llm(streaming=False)
        # 整合输入 = 采集数据拼装 + 按 user_feedback 增补（多轮最小修改 / 首轮人审意见）
        user_input = _apply_feedback(_build_synthesis_input(state), state)

        # 首选：Pydantic v2 结构化输出（function-calling），由 schema 负责字段校验与归一化
        #   （温度去单位、补齐三餐、预算自动汇总）。
        # 提示用「引导工具调用」的精简版（SYNTHESIS_STRUCTURED_PROMPT）：内嵌 JSON 示例
        #   会把 ChatTongyi 带成纯文本输出、不触发 tool_call，解析器拿不到对象只能白跑一次。
        # 注意：本版 ChatTongyi.with_structured_output 不接受 method 参数，仅支持默认的
        #   function-calling 方式；先尝试 json_mode（兼容其他模型），被拒后退回默认实现。
        try:
            structured_llm = llm.with_structured_output(TravelPlan, method="json_mode")
        except (TypeError, ValueError):
            structured_llm = llm.with_structured_output(TravelPlan)

        structured_messages = [
            SystemMessage(content=SYNTHESIS_STRUCTURED_PROMPT),
            HumanMessage(content=user_input),
        ]

        @retry(**_RETRY_KWARGS)
        async def _invoke_structured() -> TravelPlan | None:
            return await structured_llm.ainvoke(structured_messages)

        try:
            plan: TravelPlan | None = await _invoke_structured()
            if plan is None:
                # 解析器在无可用 tool_call 时返回 None —— 视为结构化失败、转入回退。
                raise ValueError("结构化输出未返回可解析的 TravelPlan")
            result = {"final_plan": plan.model_dump(), "error": None}
            logger.info(
                "node_done",
                node="synthesize",
                duration_ms=int((time.perf_counter() - t0) * 1000),
                cache_hit=False,
                parsed=True,
                mode="structured",
            )
            return result
        except Exception as structured_exc:  # noqa: BLE001
            # 回退：用详尽 JSON 提示走纯文本输出，再用 schema 校验+归一化；
            #   仅当 schema 校验也失败时，才退到容错 raw dict，保证尽量结构化。
            logger.warning("synthesize_structured_failed", error=str(structured_exc))

            fallback_messages = [
                SystemMessage(content=SYNTHESIS_AGENT_PROMPT),
                HumanMessage(content=user_input),
            ]

            @retry(**_RETRY_KWARGS)
            async def _invoke_raw() -> Any:
                return await llm.ainvoke(fallback_messages)

            raw = await _invoke_raw()
            text = getattr(raw, "content", None) or str(raw)

            plan_dict = parse_plan(text)  # schema 校验+归一化（含温度/三餐/预算）
            mode = "fallback-validated"
            if plan_dict is None:
                plan_dict = _fallback_parse(text)  # 容错兜底：原始 dict
                mode = "fallback-raw"

            logger.info(
                "node_done",
                node="synthesize",
                duration_ms=int((time.perf_counter() - t0) * 1000),
                cache_hit=False,
                parsed=plan_dict is not None,
                mode=mode,
            )

            if plan_dict is None:
                # 连容错解析也失败 —— 视为失败，交由 graph 错误分流处理。
                return {
                    "final_plan": None,
                    "error": "synthesize: 无法从模型输出中解析出有效的行程 JSON",
                    "retry_count": state.get("retry_count", 0) + 1,
                    "messages": [raw],
                }

            return {"final_plan": plan_dict, "error": None, "messages": [raw]}
    except Exception as exc:  # noqa: BLE001
        return _failure("synthesize", "final_plan", state, exc)
    finally:
        # 结构化首选 / 文本回退 / 异常分支均会经过这里，确保时延指标只观测一次。
        NODE_DURATION.labels(node="synthesize").observe(time.perf_counter() - t0)


def _fallback_parse(text: str) -> dict | None:
    """Extract JSON from mixed text —— 容错解析器（不做 schema 校验，尽量取出 dict）。"""
    import json
    import re

    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        return json.loads(match.group())
    except json.JSONDecodeError:
        return None


_GEO_LOC_RE = re.compile(r'"location"\s*:\s*"(\d{2,3}\.\d+),(\d{1,2}\.\d+)"')
_GEO_ANY_RE = re.compile(r"(\d{2,3}\.\d{3,}),(\d{1,2}\.\d{3,})")


def _parse_geo_location(text: str) -> dict | None:
    """从 maps_geo 返回里抠出第一个 'lng,lat' → {longitude, latitude}（高德 GCJ-02）。"""
    m = _GEO_LOC_RE.search(text) or _GEO_ANY_RE.search(text)
    if not m:
        return None
    return {"longitude": float(m.group(1)), "latitude": float(m.group(2))}


async def geocode_node(state: TripState) -> dict:
    """给最终计划里**缺坐标**的景点/酒店补经纬度（高德 maps_geo）。

    best-effort：DashScope 带坐标时这些项已有 location → 跳过、0 额外调用；
    高德回退（text_search 无坐标）时在此用 maps_geo 按「城市+名称」确定性补坐标，
    使地图无论主路是否可用都能打点。失败/无 geo 工具均静默跳过，不影响成稿。
    """
    plan = state.get("final_plan")
    if not plan or not plan.get("days"):
        return {}

    def _needs(obj: dict) -> bool:
        return bool(obj.get("name")) and not (obj.get("location") or {}).get("longitude")

    # 先收集缺坐标项；若没有则直接返回——不连 MCP（保持 DashScope 已带坐标/测试场景的 hermetic）。
    targets: list[dict] = []
    for day in plan["days"]:
        targets.extend(a for a in day.get("attractions", []) if _needs(a))
        hotel = day.get("hotel") or {}
        if _needs(hotel):
            targets.append(hotel)
    if not targets:
        return {}

    city = plan.get("city") or state.get("city") or ""
    t0 = time.perf_counter()
    try:
        tools = await McpClientManager().get_tools_for("geo")
        geo = next((t for t in tools if t.name == "maps_geo"), None)
        if geo is None:
            return {}

        filled = 0
        for obj in targets:
            try:
                res = await geo.ainvoke({"address": f"{city}{obj['name']}", "city": city})
                text = res if isinstance(res, str) else json.dumps(res, ensure_ascii=False)
                loc = _parse_geo_location(text)
            except Exception:  # noqa: BLE001
                loc = None
            if loc:
                obj["location"] = loc
                filled += 1
        logger.info(
            "geocode_done", filled=filled, targets=len(targets),
            duration_ms=int((time.perf_counter() - t0) * 1000), city=city,
        )
        return {"final_plan": plan} if filled else {}
    except Exception as exc:  # noqa: BLE001 —— best-effort
        logger.warning("geocode_failed", error=str(exc))
        return {}


async def error_node(state: TripState) -> dict:
    """终止节点：记录错误，明确把 final_plan 置空。"""
    logger.error("pipeline_failed", error=state.get("error"), city=state.get("city"))
    return {"error": state.get("error"), "final_plan": None}
