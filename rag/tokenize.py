"""轻量中英文分词 —— 不依赖 jieba：英文按词、中文按字二元组(bigram)。

BM25 / 词面重叠都用它。中文 bigram 在没有分词器时能给出不错的召回（"火锅"→["火锅"]，
"成都美食"→["成都","都美","美食"]），成本低、确定性强。
"""
import re

_ASCII = re.compile(r"[a-z0-9]+")
_CJK_RUN = re.compile(r"[一-鿿]+")


def tokenize(text: str) -> list[str]:
    text = (text or "").lower()
    tokens: list[str] = _ASCII.findall(text)
    for run in _CJK_RUN.findall(text):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens
