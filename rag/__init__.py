"""RAG 旅行知识层 —— 为「调 API 的 agent」补上内容深度（攻略/口碑/玩法）。

管线：问题重写(rewrite) → 多链路召回(vector + BM25 + 结构化过滤, RRF 融合) → 重排(rerank)。
所有外部依赖（embedding / rerank / LLM）都有**确定性离线兜底**，因此整套可纯本地、
免配额地跑与测试；配 DashScope（RAG_EMBEDDER=dashscope / RAG_RERANKER=dashscope）即升级为线上质量。
"""
from rag.pipeline import RagPipeline, get_default_pipeline

__all__ = ["RagPipeline", "get_default_pipeline"]
