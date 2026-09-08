"""多模型网关（A4）—— 故障转移 + 成本治理。

对应 UPGRADE_2026.md 主线 A4「多模型网关 + 成本治理」：
- 故障转移：主模型创建/调用失败时，按 settings.fallback_model_list() 逐个降级重试。
- 成本治理：估算 token 消耗、按 per-user 预算拦截、上报 Prometheus。

设计原则（与项目一贯风格一致）：
- **不引入 litellm 等重依赖**：直接复用 config.create_llm，返回契约不变（仍是 LangChain LLM）。
- **不侵入现有节点**：节点仍可直接调 create_llm；网关是可选增强层。
- **优雅降级**：无备用模型时行为与原来完全一致（只是包了一层计数）。
"""
from gateway.router import ainvoke_with_fallback, LLMGatewayError
from gateway.budget import (
    BudgetTracker,
    estimate_tokens,
    check_budget,
    record_usage,
)

__all__ = [
    "ainvoke_with_fallback",
    "LLMGatewayError",
    "BudgetTracker",
    "estimate_tokens",
    "check_budget",
    "record_usage",
]
