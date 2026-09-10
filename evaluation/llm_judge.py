"""LLM-as-judge 评估层 —— 用大模型给「答案相关性 / 忠实度」打分（2026 主流做法，取代 BLEU/ROUGE）。

关键设计：**优雅降级**。
- 有 DASHSCOPE_API_KEY：复用 config.settings.create_llm(streaming=False)，让 LLM 输出 0-1 分。
- 无 key / 调用失败：回退到 rag.eval.faithfulness 的词面覆盖代理，保证 CI 免配额也能跑、永不因判官不可用而崩。

judge 只做「打分」，不接管业务逻辑；分数聚合与门禁在 run_gate.py。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

# 注意：不要在模块顶层硬 import rag.*。rag 包会连锁导入 numpy 等重依赖，
# 在缺依赖环境会整包崩溃，从而让「本应免配额可跑」的 judge 层也无法加载。
# 这里改为惰性导入 + 纯 Python 兜底。


def tokenize(text: str) -> list[str]:
    """优先复用 rag.tokenize；不可用时退化为简单切分（中文按字、英文按词）。"""
    try:
        from rag.tokenize import tokenize as _rag_tok
        return _rag_tok(text)
    except Exception:  # noqa: BLE001
        import re as _re
        return _re.findall(r"[A-Za-z0-9]+|\S", text or "")


def _lexical_faithfulness(answer: str, contexts: list[str], threshold: float = 0.15) -> float:
    """答案忠实度词面代理：与 rag.eval.faithfulness 同口径，但内联实现以避免硬依赖 rag 包。"""
    import re as _re
    sents = [x.strip() for x in _re.split(r"[。！？\n]", answer or "") if x.strip()]
    if not sents:
        return 1.0
    ctx_tokens = [set(tokenize(c)) for c in contexts] or [set()]
    supported = 0
    for x in sents:
        st = set(tokenize(x))
        if not st:
            continue
        best = max((len(st & ct) / len(st) for ct in ctx_tokens), default=0.0)
        if best >= threshold:
            supported += 1
    return supported / len(sents)


@dataclass
class JudgeScore:
    relevancy: float        # 答案与问题的相关性 ∈ [0,1]
    faithfulness: float     # 答案是否被检索上下文支撑 ∈ [0,1]
    backend: str            # "llm" | "lexical"（标明分数来源，便于报告区分）


def _has_llm() -> bool:
    """是否具备真实 LLM 判官条件（配了 DashScope key）。

    优先走 config.settings（pydantic-settings 会读 .env），与业务侧取 key 的方式一致，
    避免「.env 配了 key 但判官仍降级为词面代理」。settings 不可用时回退到环境变量。
    """
    try:
        from config import settings
        return bool(settings.dashscope_api_key)
    except Exception:  # noqa: BLE001 —— config 不可加载时退回环境变量判断
        return bool(os.getenv("DASHSCOPE_API_KEY"))


# ── 词面代理（无 key 时的兜底判官）──────────────────────────────────
def _lexical_relevancy(question: str, answer: str) -> float:
    """相关性代理：答案对问题关键词的覆盖率（问题为空视为满分）。"""
    q = set(tokenize(question or ""))
    if not q:
        return 1.0
    a = set(tokenize(answer or ""))
    return len(q & a) / len(q)


def _lexical_judge(question: str, answer: str, contexts: list[str]) -> JudgeScore:
    return JudgeScore(
        relevancy=round(_lexical_relevancy(question, answer), 4),
        faithfulness=round(_lexical_faithfulness(answer, contexts), 4),
        backend="lexical",
    )


# ── LLM 判官 ────────────────────────────────────────────────────────
_JUDGE_PROMPT = """你是严格的评估打分员。请阅读【问题】【参考上下文】【待评答案】，
从两个维度打分，均为 0 到 1 之间的小数（保留两位）：
1. relevancy：答案是否切题、直接回应了问题。
2. faithfulness：答案的事实是否被参考上下文支撑（无上下文支撑或臆造则低分）。

只输出严格的 JSON，不要任何多余文字，格式：
{{"relevancy": <0-1>, "faithfulness": <0-1>}}

【问题】
{question}

【参考上下文】
{contexts}

【待评答案】
{answer}
"""


def _parse_scores(text: str) -> tuple[float, float] | None:
    """从 LLM 输出里稳健抽取两个分数：先试 JSON，失败再正则兜底。"""
    if not text:
        return None
    # 优先找 JSON 块
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            d = json.loads(m.group(0))
            return float(d["relevancy"]), float(d["faithfulness"])
        except (ValueError, KeyError, TypeError):
            pass
    # 正则兜底：relevancy: 0.x / faithfulness: 0.x
    r = re.search(r"relevanc\w*\D+([01](?:\.\d+)?)", text, re.IGNORECASE)
    f = re.search(r"faithful\w*\D+([01](?:\.\d+)?)", text, re.IGNORECASE)
    if r and f:
        return float(r.group(1)), float(f.group(1))
    return None


def _clip(x: float) -> float:
    return max(0.0, min(1.0, x))


def judge_one(question: str, answer: str, contexts: list[str]) -> JudgeScore:
    """对单条 (问题, 答案, 上下文) 打分；无 key 或异常时降级为词面代理。"""
    if not _has_llm():
        return _lexical_judge(question, answer, contexts)
    try:
        from config import settings

        llm = settings.create_llm(streaming=False)
        prompt = _JUDGE_PROMPT.format(
            question=question or "（空）",
            contexts="\n---\n".join(contexts) if contexts else "（无上下文）",
            answer=answer or "（空）",
        )
        resp = llm.invoke(prompt)
        text = getattr(resp, "content", None) or str(resp)
        parsed = _parse_scores(text)
        if parsed is None:
            return _lexical_judge(question, answer, contexts)
        rel, faith = parsed
        return JudgeScore(relevancy=round(_clip(rel), 4),
                          faithfulness=round(_clip(faith), 4),
                          backend="llm")
    except Exception:  # noqa: BLE001 —— 判官不可用绝不阻断评估，降级即可
        return _lexical_judge(question, answer, contexts)


def judge_batch(cases: list[dict]) -> dict:
    """批量打分。cases: [{"question", "answer", "contexts"}...]，返回均值 + 明细 + backend。"""
    if not cases:
        return {"llm_relevancy": 1.0, "llm_faithfulness": 1.0, "backend": "none", "per_case": []}
    per = []
    rel_sum = faith_sum = 0.0
    backend = "lexical"
    for c in cases:
        s = judge_one(c.get("question", ""), c.get("answer", ""), c.get("contexts", []) or [])
        backend = s.backend  # 全批一致（同一环境要么都 llm 要么都 lexical）
        rel_sum += s.relevancy
        faith_sum += s.faithfulness
        per.append({"question": c.get("question", ""), "relevancy": s.relevancy,
                    "faithfulness": s.faithfulness})
    n = len(cases)
    return {
        "llm_relevancy": round(rel_sum / n, 4),
        "llm_faithfulness": round(faith_sum / n, 4),
        "backend": backend,
        "per_case": per,
    }
