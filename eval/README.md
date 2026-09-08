# eval/ —— 统一评估门禁

对应 `UPGRADE_2026.md` 主线 A1「评估回环」的落地实现：把项目里原本分散的启发式评估器，
统一编排成一个可量化、可作为 CI 红线的门禁，并新增 **LLM-as-judge** 层（2026 主流做法，取代 BLEU/ROUGE）。

## 三层评估

| 层 | 来源 | 指标 | 是否参与门禁 |
|----|------|------|------|
| 计划质量 | 复用 `tests/eval/evaluator.py` | completeness / preference_match / budget_consistency | ✅ 始终（免配额、结果确定）|
| RAG 检索 | 复用 `rag/eval.py` | recall@4 / precision@4 / MRR | ✅ recall@4（需 numpy 等依赖）|
| LLM-as-judge | 新增 `eval/llm_judge.py` | llm_relevancy / llm_faithfulness | ⚠️ 仅当配了 `DASHSCOPE_API_KEY` 时 |

## 优雅降级设计（关键）

- **无 `DASHSCOPE_API_KEY`**：LLM 判官自动降级为词面覆盖代理，分数**仅报告、不参与门禁** ——
  避免 CI 无 key 时用弱代理误判红灯。
- **缺 numpy / RAG 环境不可用**：RAG 层自动跳过，不阻断其余评估。
- judge 层不在模块顶层硬 import `rag.*`（那会连锁引入 numpy），改为惰性导入 + 纯 Python 兜底，
  保证「本应免配额可跑」的门禁在任何环境都能加载。

## 用法

```bash
python -m eval.run_gate                    # 报告模式，退出码恒为 0（不阻塞）
python -m eval.run_gate --strict           # 门禁模式：参与门禁的指标不达标 → 退出码 1
python -m eval.run_gate --json report.json # 附带写出结构化结果（供 Grafana/看板消费）
```

配了 `DASHSCOPE_API_KEY` 后，LLM 判官启用真实大模型打分并纳入门禁：

```bash
export DASHSCOPE_API_KEY=sk-xxx
python -m eval.run_gate --strict
```

## 阈值

集中在 `eval/thresholds.py`：
- `TARGET`：2026 生产级目标（faithfulness≥0.9 / relevancy≥0.85 等）——最终收敛目标。
- `GATE`：CI 起步门禁值（略低于 TARGET，给持续改进留缓冲）。随质量提升逐步向 TARGET 收敛。

## CI 接入

`.github/workflows/ci.yml` 在 `pytest` 之后新增 `Eval gate` 步骤，跑 `python -m eval.run_gate --strict`。
分数低于 `GATE` 阈值即阻断合并。

## 评测集

- `eval/datasets/judge_qa.json`：LLM 判官用的问答集，问题与答案锚定 `rag/corpus/travel_notes.json`。
- 扩充方式：往该 JSON 追加 `{question, where, reference_answer}` 即可；`where` 用于检索时下发结构化过滤。
