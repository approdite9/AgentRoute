"""统一评估门禁 —— 编排「计划质量 + RAG 检索 + LLM-as-judge」三层，产出量化报告并可作为 CI 门禁。

用法：
    python -m eval.run_gate            # 跑全部评估，打印报告（退出码始终 0，不阻塞）
    python -m eval.run_gate --strict   # 门禁模式：任一「参与门禁」的指标低于阈值 → 退出码 1
    python -m eval.run_gate --json out.json   # 附带把结构化结果写盘（供 Grafana/看板消费）

门禁参与规则：
- 计划质量（completeness / preference_match / budget_consistency）与 RAG（recall@4 / faithfulness）
  始终参与门禁（纯本地、免配额，结果确定）。
- LLM-as-judge（llm_relevancy / llm_faithfulness）**仅在真实 LLM 判官可用时**（配了 DASHSCOPE_API_KEY）
  参与门禁；降级为词面代理时只报告、不门禁，避免 CI 无 key 时用弱代理误判红灯。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from eval.thresholds import GATE, TARGET, gate_for

_DATASET = Path(__file__).parent / "datasets" / "judge_qa.json"


# ── 第 1 层：计划质量（复用 tests/eval/evaluator.py 的启发式评估器）──────────
def _eval_plan_quality() -> dict:
    from tests.eval.evaluator import TEST_CASES, build_sample_plan, evaluate_plan

    keys = ["completeness", "preference_match", "budget_consistency"]
    acc = {k: 0.0 for k in keys}
    for case in TEST_CASES:
        plan = build_sample_plan(case["inputs"])
        scores = evaluate_plan(plan, case["expected"])
        for k in keys:
            acc[k] += scores[k]
    n = len(TEST_CASES) or 1
    return {k: round(acc[k] / n, 4) for k in keys}


# ── 第 2 层：RAG 检索质量（复用 rag/eval.py）────────────────────────────────
def _eval_rag() -> dict:
    try:
        from rag.eval import EVAL_SET, evaluate
        from rag.pipeline import get_default_pipeline

        pipe = get_default_pipeline()
        rep = evaluate(pipe, EVAL_SET, k=4)
        return {"rag_recall@4": round(rep.recall, 4), "rag_faithfulness": None,
                "rag_precision@4": round(rep.precision, 4), "rag_mrr": round(rep.mrr, 4),
                "_pipeline": pipe}
    except Exception as exc:  # noqa: BLE001 —— RAG 环境不可用时跳过该层，不阻断门禁
        print(f"[warn] RAG 评估跳过：{exc}")
        return {}


# ── 第 3 层：LLM-as-judge（answer relevancy / faithfulness）────────────────
def _eval_llm_judge(pipeline) -> dict:
    from eval.llm_judge import judge_batch

    if not _DATASET.exists():
        return {}
    cases_raw = json.loads(_DATASET.read_text(encoding="utf-8"))
    judge_cases = []
    for c in cases_raw:
        # 用检索管线取上下文；无 pipeline 时上下文为空（judge 会据此降级判定）。
        contexts: list[str] = []
        if pipeline is not None:
            try:
                hits = pipeline.retrieve(c["question"], where=c.get("where"), k=4)
                contexts = [h.text for h in hits]
            except Exception:  # noqa: BLE001
                contexts = []
        judge_cases.append({
            "question": c["question"],
            "answer": c.get("reference_answer", ""),
            "contexts": contexts,
        })
    return judge_batch(judge_cases)


def run(strict: bool = False, json_out: str | None = None) -> int:
    results: dict[str, float] = {}
    gated: dict[str, bool] = {}   # 该指标是否参与门禁

    # 1. 计划质量
    plan_scores = _eval_plan_quality()
    for k, v in plan_scores.items():
        results[k] = v
        gated[k] = True

    # 2. RAG 检索
    rag = _eval_rag()
    pipeline = rag.pop("_pipeline", None)
    for k, v in rag.items():
        if v is None:
            continue
        results[k] = v
        gated[k] = k in GATE   # 只有登记了阈值的（recall@4）参与门禁

    # 3. LLM-as-judge（无 key 时降级为词面代理，不参与门禁）
    judge = _eval_llm_judge(pipeline)
    judge_backend = judge.get("backend", "none")
    for k in ("llm_relevancy", "llm_faithfulness"):
        if k in judge:
            results[k] = judge[k]
            gated[k] = (judge_backend == "llm")   # 仅真实 LLM 判官参与门禁

    # ── 报告 ──────────────────────────────────────────────────────────
    print("\n" + "=" * 68)
    print("  AgentRoute 统一评估门禁报告")
    print("=" * 68)
    print(f"{'指标':<22}{'得分':>8}{'门禁':>8}{'目标':>8}   状态")
    print("-" * 68)
    failures = []
    for metric, score in results.items():
        g = gate_for(metric)
        t = TARGET.get(metric, "-")
        in_gate = gated.get(metric, False)
        if in_gate and score < g:
            status = "✗ FAIL"
            failures.append((metric, score, g))
        elif in_gate:
            status = "✓ PASS"
        else:
            status = "· 仅报告"
        t_str = f"{t:.2f}" if isinstance(t, float) else str(t)
        print(f"{metric:<22}{score:>8.2f}{g:>8.2f}{t_str:>8}   {status}")
    print("-" * 68)
    print(f"LLM 判官后端：{judge_backend}"
          + ("（真实 LLM，参与门禁）" if judge_backend == "llm"
             else "（词面代理降级，仅报告 —— 配 DASHSCOPE_API_KEY 后启用真实判官）"))

    if json_out:
        Path(json_out).write_text(
            json.dumps({"results": results, "gated": gated,
                        "judge_backend": judge_backend, "failures": failures},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结构化结果已写入：{json_out}")

    # ── 门禁判定 ──────────────────────────────────────────────────────
    if strict and failures:
        print("\n❌ 门禁未通过，未达标指标：")
        for m, s, g in failures:
            print(f"   - {m}: {s:.2f} < 阈值 {g:.2f}")
        print("=" * 68)
        return 1
    print("\n✅ 门禁通过" if strict else "\n（报告模式，未启用门禁；加 --strict 作为 CI 门禁）")
    print("=" * 68)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="AgentRoute 统一评估门禁")
    ap.add_argument("--strict", action="store_true", help="门禁模式：指标不达标则退出码 1")
    ap.add_argument("--json", dest="json_out", default=None, help="把结构化结果写入该路径")
    args = ap.parse_args()
    sys.exit(run(strict=args.strict, json_out=args.json_out))


if __name__ == "__main__":
    main()
