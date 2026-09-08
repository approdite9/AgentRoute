"""LLM 故障转移路由 —— 主模型失败时按 fallback 列表逐个降级重试。

复用 config.create_llm(model=...)，返回契约不变。调用方式：
    from gateway import ainvoke_with_fallback
    resp = await ainvoke_with_fallback(prompt, streaming=False)

行为：
- 依次尝试 [主模型] + settings.fallback_model_list()。
- 每个模型：create_llm(model=m) → llm.ainvoke(prompt)；成功即返回，并计成功指标。
- 失败：计错误指标，记录并切下一个（计 fallback 指标）；全部失败抛 LLMGatewayError。
- 无备用模型时，等价于「单模型 + 计数」，行为与直接调 create_llm 一致。
"""
from __future__ import annotations

import structlog

from config import settings
from monitoring.metrics import LLM_CALLS, LLM_FALLBACKS

logger = structlog.get_logger(__name__)


class LLMGatewayError(RuntimeError):
    """所有候选模型都失败时抛出，携带最后一个异常信息。"""


def _model_chain() -> list[str]:
    """主模型 + 去重后的备用模型，构成完整尝试链。"""
    chain = [settings.model_name]
    for m in settings.fallback_model_list():
        if m not in chain:
            chain.append(m)
    return chain


async def ainvoke_with_fallback(prompt, *, streaming: bool = False):
    """按模型链依次尝试 LLM 调用，返回首个成功的响应对象（LangChain 消息）。

    prompt 可为 str 或 LangChain 消息列表（与 llm.ainvoke 接受的一致）。
    全部失败抛 LLMGatewayError。
    """
    chain = _model_chain()
    last_exc: Exception | None = None
    prev_model: str | None = None

    for model in chain:
        if prev_model is not None:
            LLM_FALLBACKS.labels(from_model=prev_model, to_model=model).inc()
            logger.warning("llm_fallback", from_model=prev_model, to_model=model)
        try:
            llm = settings.create_llm(streaming=streaming, model=model)
            resp = await llm.ainvoke(prompt)
            LLM_CALLS.labels(model=model, status="success").inc()
            return resp
        except Exception as exc:  # noqa: BLE001 —— 逐个模型兜底，最终统一抛出
            LLM_CALLS.labels(model=model, status="error").inc()
            logger.warning("llm_call_failed", model=model, error=str(exc))
            last_exc = exc
            prev_model = model

    raise LLMGatewayError(
        f"all models failed ({' -> '.join(chain)}): {last_exc}"
    ) from last_exc
