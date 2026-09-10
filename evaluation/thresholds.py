"""评估阈值 —— 集中管理，便于 CI 门禁与本地调优共用同一套标准。

阈值来源：UPGRADE_2026.md 主线 A1 的 2026 生产级目标
（faithfulness ≥ 0.9 / answer relevancy ≥ 0.85 / context precision ≥ 0.8）。
本项目在此基础上补充「计划质量」维度（completeness / preference_match / budget）。

CI 起步阶段用相对宽松的门禁值（GATE_*），避免刚接入就红；
随质量提升逐步向 TARGET_* 收敛（在 PR 里调高一档即可）。
"""
from __future__ import annotations

# ── 目标值（2026 生产级，最终收敛目标）───────────────────────────────
TARGET = {
    "completeness": 0.90,
    "preference_match": 0.85,
    "budget_consistency": 1.00,
    "rag_recall@4": 0.85,
    # context precision（UPGRADE_2026 A1 承诺 ≥0.8）：用检索 precision@4 作为等价度量。
    "rag_precision@4": 0.80,
    "rag_faithfulness": 0.90,
    "llm_relevancy": 0.85,
    "llm_faithfulness": 0.90,
}

# ── 门禁值（CI 起步阶段，低于此值才判失败）─────────────────────────────
# 略低于 TARGET，给持续改进留缓冲；LLM 类指标在无 key 时不参与门禁（见 run_gate）。
GATE = {
    "completeness": 0.80,
    "preference_match": 0.75,
    "budget_consistency": 1.00,
    "rag_recall@4": 0.75,
    # 起步门禁略低于 target；precision@4 受语料规模影响，起步给 0.40 缓冲。
    "rag_precision@4": 0.40,
    "rag_faithfulness": 0.60,
    "llm_relevancy": 0.75,
    "llm_faithfulness": 0.80,
}


def gate_for(metric: str) -> float:
    """取某指标的门禁阈值；未登记的指标默认 0.0（不门禁）。"""
    return GATE.get(metric, 0.0)
