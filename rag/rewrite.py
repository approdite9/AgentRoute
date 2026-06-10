"""问题重写(query rewriting) —— 口语化/含糊的用户需求扩写成多条检索友好的查询。

提升召回覆盖：multi-query（多角度改写）+ 可选 HyDE（先让 LLM 写一段"假设答案"，
用它去检索，往往比原始短查询更贴近文档语言）。无 LLM 时退回原查询，保证可离线测试。
"""
from __future__ import annotations


def rewrite_query(query: str, llm=None, n: int = 3, hyde: bool = False) -> list[str]:
    """返回查询变体列表（含原查询）。llm=None 时仅返回 [query]（离线兜底）。"""
    query = (query or "").strip()
    if not query:
        return []
    if llm is None:
        return [query]
    try:
        variants = _llm_rewrite(query, llm, n)
    except Exception:  # noqa: BLE001 —— 改写失败不阻断检索
        return [query]
    out = [query] + [v for v in variants if v and v != query]
    if hyde:
        try:
            out.append(_hyde(query, llm))
        except Exception:  # noqa: BLE001
            pass
    # 去重保序
    seen, uniq = set(), []
    for q in out:
        if q not in seen:
            seen.add(q)
            uniq.append(q)
    return uniq


def _llm_rewrite(query: str, llm, n: int) -> list[str]:
    from langchain_core.messages import HumanMessage, SystemMessage

    sys = (
        "你是检索查询改写器。把用户的旅行需求改写成 %d 条**角度不同、更利于检索攻略/百科**的查询，"
        "每行一条，只输出查询本身，不要编号或解释。" % n
    )
    resp = llm.invoke([SystemMessage(content=sys), HumanMessage(content=query)])
    text = getattr(resp, "content", "") or str(resp)
    return [ln.strip(" -*\t") for ln in text.splitlines() if ln.strip()][:n]


def _hyde(query: str, llm) -> str:
    from langchain_core.messages import HumanMessage, SystemMessage

    sys = "针对用户的旅行问题，写一段 2-3 句的、像攻略片段一样的'假设答案'，用于检索。只输出这段文字。"
    resp = llm.invoke([SystemMessage(content=sys), HumanMessage(content=query)])
    return (getattr(resp, "content", "") or "").strip()
