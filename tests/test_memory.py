"""长期记忆层（A2）测试 —— 无需 Redis：不可用时降级为内存兜底，逻辑仍可验证。

覆盖：
- merge_memory_into_state：空字段补全、不覆盖用户本次明确输入、list/标量分别处理。
- update_memory_from_state：list 累积去重、标量更新、空值不抹除、visit_count 递增。
- 降级路径：Redis 不可用时读写走内存兜底，get/save 往返一致。
"""
from __future__ import annotations

import pytest

from memory.store import (
    merge_memory_into_state,
    _merge_field,
    get_user_memory,
    save_user_memory,
    update_memory_from_state,
    LONG_TERM_PREF_FIELDS,
)

pytestmark = pytest.mark.anyio


# ---------- 纯函数：merge_memory_into_state ----------

def test_merge_fills_empty_scalar_fields():
    state = {"hotel_type": "", "origin_city": None, "city": "北京"}
    memory = {"hotel_type": "舒适型", "origin_city": "成都"}
    merged = merge_memory_into_state(state, memory)
    assert merged["hotel_type"] == "舒适型"
    assert merged["origin_city"] == "成都"
    # 非偏好字段不受影响
    assert merged["city"] == "北京"


def test_merge_does_not_override_user_input():
    """用户本次明确填了的字段，记忆不得覆盖。"""
    state = {"hotel_type": "经济型", "party_type": "情侣出行"}
    memory = {"hotel_type": "高端奢华", "party_type": "家庭亲子"}
    merged = merge_memory_into_state(state, memory)
    assert merged["hotel_type"] == "经济型"
    assert merged["party_type"] == "情侣出行"


def test_merge_fills_empty_list_fields_only():
    state = {"preferences": [], "transport": ["高铁"]}
    memory = {"preferences": ["美食", "历史文化"], "transport": ["地铁"]}
    merged = merge_memory_into_state(state, memory)
    assert merged["preferences"] == ["美食", "历史文化"]  # 空 → 用记忆
    assert merged["transport"] == ["高铁"]                # 非空 → 保留用户输入


def test_merge_empty_memory_returns_copy():
    state = {"hotel_type": "经济型"}
    merged = merge_memory_into_state(state, {})
    assert merged == state
    assert merged is not state  # 返回副本，不原地改


# ---------- 纯函数：_merge_field ----------

def test_merge_field_list_dedup_preserves_order():
    assert _merge_field("preferences", ["美食"], ["美食", "购物"]) == ["美食", "购物"]


def test_merge_field_scalar_new_wins_but_empty_keeps_old():
    assert _merge_field("hotel_type", "经济型", "舒适型") == "舒适型"
    assert _merge_field("hotel_type", "经济型", "") == "经济型"


# ---------- 降级往返：get/save（无 Redis 时走内存兜底）----------

async def test_save_and_get_roundtrip():
    uid = "test-user-roundtrip"
    mem = {"hotel_type": "舒适型", "preferences": ["美食"], "visit_count": 1}
    await save_user_memory(uid, mem)
    got = await get_user_memory(uid)
    assert got.get("hotel_type") == "舒适型"
    assert got.get("preferences") == ["美食"]


async def test_get_unknown_user_returns_empty():
    got = await get_user_memory("nonexistent-user-xyz")
    assert isinstance(got, dict)


async def test_empty_user_id_is_noop():
    assert await get_user_memory("") == {}
    await save_user_memory("", {"x": 1})  # 不应抛异常
    assert await update_memory_from_state("", {"hotel_type": "x"}) == {}


# ---------- 回写累积：update_memory_from_state ----------

async def test_update_accumulates_and_increments_visit():
    uid = "test-user-accumulate"
    # 第一次：家庭亲子 + 美食
    m1 = await update_memory_from_state(uid, {
        "preferences": ["美食"], "party_type": "家庭亲子", "hotel_type": "舒适型",
    })
    assert m1["visit_count"] == 1
    assert m1["preferences"] == ["美食"]
    # 第二次：追加 历史文化，改 hotel_type，party_type 空值不覆盖
    m2 = await update_memory_from_state(uid, {
        "preferences": ["历史文化"], "party_type": "", "hotel_type": "高端奢华",
    })
    assert m2["visit_count"] == 2
    assert m2["preferences"] == ["美食", "历史文化"]   # 累积去重
    assert m2["hotel_type"] == "高端奢华"              # 标量更新
    assert m2["party_type"] == "家庭亲子"              # 空值不抹除历史


async def test_update_empty_state_still_increments_visit():
    uid = "test-user-emptystate"
    m = await update_memory_from_state(uid, {"city": "北京"})  # 无偏好字段
    assert m["visit_count"] == 1
    assert "updated_at" in m


def test_long_term_fields_are_stable():
    """字段清单是长期记忆契约，防止误改。"""
    assert set(LONG_TERM_PREF_FIELDS) == {
        "preferences", "transport", "hotel_type",
        "origin_city", "party_type", "budget_level",
    }
