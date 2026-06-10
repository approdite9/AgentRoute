"""RAG 评估 —— 检索质量（recall@k / precision@k / MRR / hit_rate）+ 答案忠实度(faithfulness)。

相关性按「城市 + 类别」粒度判定（与具体切片边界无关，评测稳定）。faithfulness 用
轻量词面覆盖做代理（答案的句子是否被检索上下文支撑），并预留 LLM-as-judge 钩子。
配合 RAGAS/LangSmith 可平滑升级，但本模块**纯本地、免配额**即可跑回归。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from rag.chunk import Chunk, meta_match
from rag.tokenize import tokenize


# ---- 评测集：每条 query 给出结构化过滤 where 与"相关性判据"relevant(meta)->bool ----
@dataclass
class EvalCase:
    query: str
    relevant: dict          # 命中视为相关的元数据条件（城市[+类别]）
    where: dict | None = None  # 检索时下发的结构化过滤（可选）


EVAL_SET: list[EvalCase] = [
    EvalCase("成都有什么好吃的", {"city": "成都", "category": "美食"}, {"city": "成都"}),
    EvalCase("成都看熊猫怎么安排", {"city": "成都", "category": "玩法"}, {"city": "成都"}),
    EvalCase("成都节假日要注意什么", {"city": "成都", "category": "避坑"}, {"city": "成都"}),
    EvalCase("长沙夜宵吃什么", {"city": "长沙", "category": "美食"}, {"city": "长沙"}),
    EvalCase("长沙有哪些必去景点", {"city": "长沙", "category": "玩法"}, {"city": "长沙"}),
    EvalCase("三亚浮潜出海", {"city": "三亚", "category": "海滨"}, {"city": "三亚"}),
    EvalCase("三亚吃海鲜怎么不被宰", {"city": "三亚", "category": "避坑"}, {"city": "三亚"}),
    EvalCase("北京故宫长城行程", {"city": "北京", "category": "历史文化"}, {"city": "北京"}),
]


def _is_relevant(chunk: Chunk, relevant: dict) -> bool:
    return meta_match(chunk.meta, relevant)


def recall_at_k(retrieved: list[Chunk], relevant: dict, k: int) -> float:
    """命中率/recall@k：top-k 里是否至少召回到一个相关块（1.0/0.0）。"""
    return 1.0 if any(_is_relevant(c, relevant) for c in retrieved[:k]) else 0.0


def precision_at_k(retrieved: list[Chunk], relevant: dict, k: int) -> float:
    top = retrieved[:k]
    if not top:
        return 0.0
    return sum(_is_relevant(c, relevant) for c in top) / len(top)


def mrr(retrieved: list[Chunk], relevant: dict) -> float:
    for i, c in enumerate(retrieved):
        if _is_relevant(c, relevant):
            return 1.0 / (i + 1)
    return 0.0


def faithfulness(answer: str, contexts: list[str], threshold: float = 0.15) -> float:
    """答案忠实度代理：答案的句子中，能被某条上下文以词面覆盖支撑的比例。"""
    import re

    sents = [s.strip() for s in re.split(r"[。！？\n]", answer or "") if s.strip()]
    if not sents:
        return 1.0
    ctx_tokens = [set(tokenize(c)) for c in contexts] or [set()]
    supported = 0
    for s in sents:
        st = set(tokenize(s))
        if not st:
            continue
        best = max((len(st & ct) / len(st) for ct in ctx_tokens), default=0.0)
        if best >= threshold:
            supported += 1
    return supported / len(sents)


@dataclass
class EvalReport:
    k: int
    recall: float
    precision: float
    mrr: float
    n: int
    per_case: list[dict] = field(default_factory=list)


def evaluate(pipeline, cases: list[EvalCase] | None = None, k: int = 4) -> EvalReport:
    """对评测集逐条检索并打分，返回均值报告。"""
    cases = cases or EVAL_SET
    rec = prec = rr = 0.0
    per: list[dict] = []
    for c in cases:
        hits = pipeline.retrieve(c.query, where=c.where, k=k)
        r, p, m = recall_at_k(hits, c.relevant, k), precision_at_k(hits, c.relevant, k), mrr(hits, c.relevant)
        rec, prec, rr = rec + r, prec + p, rr + m
        per.append({"query": c.query, "recall": r, "precision": round(p, 2), "mrr": round(m, 2),
                    "top": [h.meta.get("title") for h in hits[:k]]})
    n = len(cases) or 1
    return EvalReport(k=k, recall=rec / n, precision=prec / n, mrr=rr / n, n=len(cases), per_case=per)


def benchmark(pipeline, cases: list[EvalCase] | None = None, ks=(1, 3, 5)) -> dict:
    """更完善的量化报告：①不同 k 的 recall/MRR；②延迟(mean/p95)；③消融对比
    （全量 vs 仅向量单路 vs 不重排），用于指出后续优化方向。"""
    import time

    cases = cases or EVAL_SET

    def _run(multipath: bool, rerank_on: bool, k: int) -> dict:
        rec = rr = 0.0
        lat: list[float] = []
        for c in cases:
            t0 = time.perf_counter()
            hits = pipeline.retrieve(c.query, where=c.where, k=k, multipath=multipath, rerank_on=rerank_on)
            lat.append((time.perf_counter() - t0) * 1000)
            rec += recall_at_k(hits, c.relevant, k)
            rr += mrr(hits, c.relevant)
        n = len(cases) or 1
        lat.sort()
        return {
            "recall": round(rec / n, 3), "mrr": round(rr / n, 3),
            "lat_ms_mean": round(sum(lat) / len(lat), 1),
            "lat_ms_p95": round(lat[min(len(lat) - 1, int(len(lat) * 0.95))], 1),
        }

    return {
        "n": len(cases),
        "recall_at_k": {f"recall@{k}": _run(True, True, k)["recall"] for k in ks},
        "configs": {
            "full(multipath+rerank)": _run(True, True, max(ks)),
            "vector_only": _run(False, True, max(ks)),
            "no_rerank": _run(True, False, max(ks)),
        },
    }


if __name__ == "__main__":  # 本地跑：python -m rag.eval
    from rag.pipeline import get_default_pipeline

    pipe = get_default_pipeline()
    rep = evaluate(pipe, k=4)
    print(f"\n=== RAG 检索评估 (k={rep.k}, n={rep.n}) ===")
    print(f"recall@{rep.k}={rep.recall:.2f}  precision@{rep.k}={rep.precision:.2f}  MRR={rep.mrr:.2f}")
    for row in rep.per_case:
        print(f"  recall={row['recall']:.0f} mrr={row['mrr']:.2f}  {row['query']}  -> {row['top']}")

    bench = benchmark(pipe)
    print(f"\n=== 基准 / 消融 (n={bench['n']}) ===")
    print("recall@k:", bench["recall_at_k"])
    print(f"{'config':<26}{'recall':>8}{'mrr':>8}{'lat_mean(ms)':>14}{'lat_p95(ms)':>13}")
    for name, m in bench["configs"].items():
        print(f"{name:<26}{m['recall']:>8}{m['mrr']:>8}{m['lat_ms_mean']:>14}{m['lat_ms_p95']:>13}")
