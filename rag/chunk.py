"""文档切片 —— 按句子边界 + 重叠窗口做语义友好的切片，并带上结构化元数据。

重叠(overlap)避免把"上半句在 A 块、下半句在 B 块"的语义割裂；元数据(city/category)
既用于检索时的结构化过滤，也用于评估时判定相关性。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# 句子切分：中文标点 / 换行作边界（保留标点）。
_SENT = re.compile(r"[^。！？；;\n]+[。！？；;\n]?")


@dataclass
class Chunk:
    id: str
    text: str
    meta: dict = field(default_factory=dict)


def chunk_text(text: str, size: int = 220, overlap: int = 50) -> list[str]:
    """把长文本按句子贪心打包成 ~size 字的块，块间保留 overlap 字重叠。"""
    text = (text or "").strip()
    if not text:
        return []
    sents = [s.strip() for s in _SENT.findall(text) if s.strip()]
    if not sents:
        return [text]
    chunks: list[str] = []
    cur = ""
    for s in sents:
        if cur and len(cur) + len(s) > size:
            chunks.append(cur)
            tail = cur[-overlap:] if overlap else ""  # 携带上一块尾部做重叠
            cur = tail + s
        else:
            cur += s
    if cur.strip():
        chunks.append(cur)
    return chunks


def chunk_documents(docs: list[dict], size: int = 220, overlap: int = 50) -> list[Chunk]:
    """docs: [{doc_id, city, category, title, text}] → 带元数据的 Chunk 列表。"""
    out: list[Chunk] = []
    for d in docs:
        pieces = chunk_text(d.get("text", ""), size=size, overlap=overlap)
        for i, piece in enumerate(pieces):
            out.append(
                Chunk(
                    id=f"{d['doc_id']}#{i}",
                    text=piece,
                    meta={
                        "doc_id": d["doc_id"],
                        "city": d.get("city", ""),
                        "category": d.get("category", ""),
                        "title": d.get("title", ""),
                    },
                )
            )
    return out


def meta_match(meta: dict, where: dict | None) -> bool:
    """结构化过滤：where 的每个键都要命中（值相等，或值在给定列表内）。"""
    if not where:
        return True
    for key, want in where.items():
        got = meta.get(key)
        if isinstance(want, (list, tuple, set)):
            if got not in want:
                return False
        elif got != want:
            return False
    return True
