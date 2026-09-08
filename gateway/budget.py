"""成本治理 —— token 估算 + per-user 预算 + Prometheus 上报。

无 tiktoken 等重依赖：token 用「字符启发式」估算（中文≈1.7 char/token，英文≈4 char/token
的折中，取 len/2 上取整作为保守估计），足够做预算护栏与相对趋势观测；
需要精确计费时可替换 estimate_tokens 的实现。

预算按 user_id 累计（进程内 BudgetTracker），超 settings.token_budget_per_user 时 check_budget
返回 False，由调用方决定拒绝/降级。token_budget_per_user=0 表示不限制。
"""
from __future__ import annotations

import math

import structlog

from config import settings
from monitoring.metrics import LLM_TOKENS, LLM_BUDGET_BLOCKS

logger = structlog.get_logger(__name__)


def estimate_tokens(text: str) -> int:
    """保守估算文本 token 数（字符启发式，无外部依赖）。空串为 0。"""
    if not text:
        return 0
    # 取 len/2 上取整：中英文混合下的保守上界，宁可高估以免预算穿透。
    return math.ceil(len(text) / 2)


class BudgetTracker:
    """进程内 per-user token 累计器。生产多进程下可换 Redis 后端，接口不变。"""

    def __init__(self) -> None:
        self._used: dict[str, int] = {}

    def get(self, user_id: str) -> int:
        return self._used.get(user_id, 0)

    def add(self, user_id: str, tokens: int) -> int:
        self._used[user_id] = self._used.get(user_id, 0) + max(0, tokens)
        return self._used[user_id]

    def reset(self, user_id: str) -> None:
        self._used.pop(user_id, None)


# 模块级默认 tracker（单进程/CLI/测试足够；生产可注入自定义实例）。
_DEFAULT_TRACKER = BudgetTracker()


def check_budget(user_id: str, prompt: str, tracker: BudgetTracker | None = None) -> bool:
    """预算护栏：若加上本次 prompt 的估算 token 会超 per-user 预算，返回 False（应拒绝）。

    token_budget_per_user=0 或 user_id 为空时不限制，恒返回 True。
    """
    budget = settings.token_budget_per_user
    if not budget or not user_id:
        return True
    tracker = tracker or _DEFAULT_TRACKER
    projected = tracker.get(user_id) + estimate_tokens(prompt)
    if projected > budget:
        LLM_BUDGET_BLOCKS.inc()
        logger.warning("llm_budget_exceeded", user_id=user_id,
                       used=tracker.get(user_id), projected=projected, budget=budget)
        return False
    return True


def record_usage(
    user_id: str, prompt: str, completion: str, tracker: BudgetTracker | None = None
) -> dict:
    """记录一次调用的 token 消耗：上报 Prometheus + 累计到 user 预算。返回本次用量。"""
    p_tok = estimate_tokens(prompt)
    c_tok = estimate_tokens(completion)
    LLM_TOKENS.labels(kind="prompt").inc(p_tok)
    LLM_TOKENS.labels(kind="completion").inc(c_tok)
    if user_id:
        (tracker or _DEFAULT_TRACKER).add(user_id, p_tok + c_tok)
    return {"prompt_tokens": p_tok, "completion_tokens": c_tok, "total": p_tok + c_tok}
