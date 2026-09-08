"""多模型网关（A4）测试 —— 故障转移 + 成本治理。

LLM 调用通过 monkeypatch config.Settings.create_llm 注入假模型（与 test_graph 一致），
无需真实 API key。预算/token 估算为纯函数，直接验证。
"""
from __future__ import annotations

import pytest

import config
from gateway.router import ainvoke_with_fallback, LLMGatewayError, _model_chain
from gateway.budget import estimate_tokens, BudgetTracker, check_budget, record_usage

pytestmark = pytest.mark.anyio


# ---------- 假 LLM ----------

class _FakeResp:
    def __init__(self, content):
        self.content = content


class _OkLLM:
    def __init__(self, model):
        self.model = model

    async def ainvoke(self, prompt):
        return _FakeResp(f"ok from {self.model}")


class _BrokenLLM:
    def __init__(self, model):
        self.model = model

    async def ainvoke(self, prompt):
        raise RuntimeError(f"{self.model} down")


# ---------- 故障转移 ----------

async def test_primary_success_no_fallback(monkeypatch):
    monkeypatch.setattr(config.settings, "model_name", "primary")
    monkeypatch.setattr(config.settings, "fallback_models", "")
    monkeypatch.setattr(config.Settings, "create_llm",
                        lambda self, *, streaming=False, model=None: _OkLLM(model))
    resp = await ainvoke_with_fallback("hi")
    assert resp.content == "ok from primary"


async def test_falls_back_to_second_model(monkeypatch):
    monkeypatch.setattr(config.settings, "model_name", "primary")
    monkeypatch.setattr(config.settings, "fallback_models", "backup1,backup2")

    def make(self, *, streaming=False, model=None):
        # 主模型坏，backup1 好
        return _BrokenLLM(model) if model == "primary" else _OkLLM(model)

    monkeypatch.setattr(config.Settings, "create_llm", make)
    resp = await ainvoke_with_fallback("hi")
    assert resp.content == "ok from backup1"


async def test_all_models_fail_raises(monkeypatch):
    monkeypatch.setattr(config.settings, "model_name", "primary")
    monkeypatch.setattr(config.settings, "fallback_models", "backup1")
    monkeypatch.setattr(config.Settings, "create_llm",
                        lambda self, *, streaming=False, model=None: _BrokenLLM(model))
    with pytest.raises(LLMGatewayError):
        await ainvoke_with_fallback("hi")


def test_model_chain_dedup(monkeypatch):
    monkeypatch.setattr(config.settings, "model_name", "primary")
    monkeypatch.setattr(config.settings, "fallback_models", "primary,backup1,backup1")
    # 主模型与备用去重，保序
    assert _model_chain() == ["primary", "backup1"]


# ---------- 成本治理 ----------

def test_estimate_tokens_monotonic():
    assert estimate_tokens("") == 0
    assert estimate_tokens("a" * 10) == 5
    assert estimate_tokens("成都三日游") > 0


def test_budget_tracker_accumulates():
    t = BudgetTracker()
    assert t.get("u1") == 0
    t.add("u1", 100)
    t.add("u1", 50)
    assert t.get("u1") == 150
    t.reset("u1")
    assert t.get("u1") == 0


def test_check_budget_unlimited_when_zero(monkeypatch):
    monkeypatch.setattr(config.settings, "token_budget_per_user", 0)
    assert check_budget("u1", "x" * 10000) is True


def test_check_budget_blocks_over_limit(monkeypatch):
    monkeypatch.setattr(config.settings, "token_budget_per_user", 100)
    t = BudgetTracker()
    t.add("u1", 90)
    # 已用 90，再来 estimate_tokens("x"*40)=20 → 110 > 100 → 拦截
    assert check_budget("u1", "x" * 40, tracker=t) is False


def test_check_budget_allows_within_limit(monkeypatch):
    monkeypatch.setattr(config.settings, "token_budget_per_user", 100)
    t = BudgetTracker()
    assert check_budget("u1", "x" * 40, tracker=t) is True


def test_record_usage_accumulates(monkeypatch):
    monkeypatch.setattr(config.settings, "token_budget_per_user", 1000)
    t = BudgetTracker()
    usage = record_usage("u1", "prompt" * 10, "completion" * 5, tracker=t)
    assert usage["total"] == usage["prompt_tokens"] + usage["completion_tokens"]
    assert t.get("u1") == usage["total"]
