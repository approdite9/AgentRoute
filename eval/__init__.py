"""统一评估门禁包 —— 编排「计划质量」+「RAG 检索」+「LLM-as-judge」三层评估。

设计目标（贴合项目现有风格）：
- 纯本地、免配额即可跑：无 DASHSCOPE_API_KEY 时 LLM-as-judge 自动降级为启发式，CI 不阻塞。
- 有 key 时启用 LLM-as-judge（复用 config.settings.create_llm）。
- 阈值集中在 thresholds.py；run_gate.py --strict 才作为 CI 门禁（分数不达标 exit 1）。

对外入口：
    python -m eval.run_gate            # 跑全部评估，打印报告（不阻塞）
    python -m eval.run_gate --strict   # 作为门禁：任一指标低于阈值 → exit 1
"""
