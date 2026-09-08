"""评估门禁冒烟测试 —— 确保 eval 包在缺 numpy / 无 API key 的 CI 环境也能加载与运行。

这些测试**不依赖** numpy / redis / 大模型，验证的是门禁框架本身的健壮性：
- LLM 判官在无 key 时降级为词面代理，且分数落在 [0,1]。
- 阈值表自洽（GATE ≤ TARGET）。
- run_gate.run() 报告模式退出码为 0（不阻塞）。
"""
from __future__ import annotations

from eval import thresholds
from eval.llm_judge import JudgeScore, judge_batch, judge_one


def test_thresholds_gate_not_above_target():
    """门禁阈值不应高于目标值（否则门禁比目标还严，逻辑矛盾）。"""
    for metric, gate in thresholds.GATE.items():
        target = thresholds.TARGET.get(metric)
        assert target is not None, f"{metric} 缺少 TARGET"
        assert gate <= target + 1e-9, f"{metric}: GATE {gate} > TARGET {target}"


def test_gate_for_unknown_metric_returns_zero():
    assert thresholds.gate_for("不存在的指标") == 0.0


def test_judge_degrades_without_key(monkeypatch):
    """无 DASHSCOPE_API_KEY 时应降级为词面代理，分数合法。"""
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    score = judge_one(
        question="成都火锅推荐什么锅底？",
        answer="成都火锅以牛油锅底最地道，外地肠胃建议清油锅底。",
        contexts=["成都的火锅以牛油锅底最为地道，推荐选清油锅底更适合外地肠胃。"],
    )
    assert isinstance(score, JudgeScore)
    assert score.backend == "lexical"
    assert 0.0 <= score.relevancy <= 1.0
    assert 0.0 <= score.faithfulness <= 1.0
    # 答案与上下文高度重合，忠实度应明显 > 0
    assert score.faithfulness > 0.0


def test_judge_batch_empty_is_perfect():
    out = judge_batch([])
    assert out["llm_relevancy"] == 1.0
    assert out["llm_faithfulness"] == 1.0


def test_run_gate_report_mode_returns_zero(monkeypatch):
    """报告模式（非 strict）应始终退出码 0，不阻塞。"""
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    from eval.run_gate import run

    code = run(strict=False)
    assert code == 0


def test_run_gate_strict_passes_on_plan_quality(monkeypatch):
    """strict 模式下，免配额的计划质量指标达标即应通过（RAG/LLM 缺环境时不误判红）。"""
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    from eval.run_gate import run

    code = run(strict=True)
    assert code == 0
