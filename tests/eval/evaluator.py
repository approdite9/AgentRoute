"""
TASK F —— 行程规划质量评估（LangSmith 兼容 + 可纯本地运行）。

三个评估器（均为纯函数，输入「计划 dict + 期望 dict」，输出 {key, score} ∈ [0,1]）：
  1. completeness_evaluator   —— (同时含三餐且有酒店的天数) / 总天数
  2. preference_match_evaluator —— |期望类别 ∩ 计划中出现的类别| / |期望类别|
  3. budget_consistency_evaluator —— budget.total == 各分项之和 ? 1.0 : 0.0

两种用法：
  - 本地（无需 LangSmith 账号）：
        python tests/eval/evaluator.py
    会用内置的 build_sample_plan 为每个 TEST_CASE 合成一份计划并打分（端到端演示评估流水线）。
    把 build_sample_plan 换成真实的图调用即可评估真实输出。
  - LangSmith：make_langsmith_evaluators() 返回适配 langsmith.evaluate(...) 的评估器列表，
    run_langsmith_eval() 在配置了 LANGCHAIN_API_KEY 时把 TEST_CASES 推送为数据集并评估。

注：本模块文件名不以 test_ 开头，pytest 不会把它当用例收集；它是评估脚本/库。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Callable

# ==================== 本地测试数据集 ====================
# 覆盖：短途 / 长途 / 海滨 / 山岳 / 都市短假。
TEST_CASES: list[dict[str, Any]] = [
    {
        "name": "short_trip",
        "inputs": {
            "city": "北京",
            "start_date": "2026-06-01",
            "end_date": "2026-06-03",
            "preferences": ["历史文化"],
            "hotel_type": "经济型",
            "transport": ["地铁"],
            "extra": "",
        },
        "expected": {"min_days": 2, "required_categories": ["历史文化"]},
    },
    {
        "name": "long_trip",
        "inputs": {
            "city": "成都",
            "start_date": "2026-07-01",
            "end_date": "2026-07-08",
            "preferences": ["美食", "休闲"],
            "hotel_type": "舒适型",
            "transport": ["地铁", "步行"],
            "extra": "希望节奏放松一些",
        },
        "expected": {"min_days": 7, "required_categories": ["美食", "休闲"]},
    },
    {
        "name": "beach",
        "inputs": {
            "city": "三亚",
            "start_date": "2026-08-10",
            "end_date": "2026-08-14",
            "preferences": ["海滨", "度假"],
            "hotel_type": "海景度假",
            "transport": ["打车"],
            "extra": "想多安排海边活动",
        },
        "expected": {"min_days": 4, "required_categories": ["海滨"]},
    },
    {
        "name": "mountain",
        "inputs": {
            "city": "黄山",
            "start_date": "2026-09-20",
            "end_date": "2026-09-23",
            "preferences": ["自然风光", "登山"],
            "hotel_type": "山景民宿",
            "transport": ["缆车", "步行"],
            "extra": "需要预留登山体力",
        },
        "expected": {"min_days": 3, "required_categories": ["自然风光"]},
    },
    {
        "name": "city_break",
        "inputs": {
            "city": "上海",
            "start_date": "2026-10-01",
            "end_date": "2026-10-03",
            "preferences": ["都市", "购物"],
            "hotel_type": "市中心商务",
            "transport": ["地铁"],
            "extra": "",
        },
        "expected": {"min_days": 2, "required_categories": ["都市"]},
    },
]


# ==================== 工具函数 ====================

_MEAL_TYPES = {"breakfast", "lunch", "dinner"}


def _day_is_complete(day: dict) -> bool:
    """某天「完整」= 含早/午/晚三餐 且 当天有带名字的酒店。"""
    meal_types = {m.get("type") for m in (day.get("meals") or [])}
    has_three_meals = _MEAL_TYPES.issubset(meal_types)
    hotel = day.get("hotel") or {}
    has_hotel = bool(hotel.get("name"))
    return has_three_meals and has_hotel


def _plan_categories(plan: dict) -> set[str]:
    """收集计划中所有景点出现过的类别（去空白）。"""
    cats: set[str] = set()
    for day in plan.get("days") or []:
        for attr in day.get("attractions") or []:
            cat = (attr.get("category") or "").strip()
            if cat:
                cats.add(cat)
    return cats


def _expected_categories(expected: dict | None, plan: dict) -> list[str]:
    """期望命中的偏好类别：优先取 expected.required_categories，兜底用计划自带偏好。"""
    if expected and expected.get("required_categories"):
        return list(expected["required_categories"])
    return list(plan.get("preferences") or [])


# ==================== 三个评估器 ====================

def completeness_evaluator(plan: dict, expected: dict | None = None) -> dict:
    """完整度 = (含三餐且有酒店的天数) / 总天数。"""
    days = plan.get("days") or []
    total = len(days)
    if total == 0:
        return {"key": "completeness", "score": 0.0}
    complete = sum(1 for d in days if _day_is_complete(d))
    return {"key": "completeness", "score": complete / total}


def preference_match_evaluator(plan: dict, expected: dict | None = None) -> dict:
    """偏好匹配 = |期望类别 ∩ 计划出现类别| / |期望类别|。"""
    pref_set = set(_expected_categories(expected, plan))
    if not pref_set:
        return {"key": "preference_match", "score": 1.0}  # 无偏好约束视为满分
    category_set = _plan_categories(plan)
    matched = pref_set & category_set
    return {"key": "preference_match", "score": len(matched) / len(pref_set)}


def budget_consistency_evaluator(plan: dict, expected: dict | None = None) -> dict:
    """预算自洽 = total 是否等于四个分项之和（容许 1 分钱浮点误差）。"""
    budget = plan.get("budget") or {}
    components = (
        budget.get("total_attractions", 0)
        + budget.get("total_hotels", 0)
        + budget.get("total_meals", 0)
        + budget.get("total_transportation", 0)
    )
    total = budget.get("total", 0)
    consistent = abs(total - components) < 0.01
    return {"key": "budget_consistency", "score": 1.0 if consistent else 0.0}


EVALUATORS: list[Callable[[dict, dict | None], dict]] = [
    completeness_evaluator,
    preference_match_evaluator,
    budget_consistency_evaluator,
]


def evaluate_plan(plan: dict, expected: dict | None = None) -> dict[str, float]:
    """对单份计划跑全部评估器，返回 {评估名: 分数}。"""
    return {ev(plan, expected)["key"]: ev(plan, expected)["score"] for ev in EVALUATORS}


# ==================== 本地合成计划（离线演示用） ====================

def _days_between(start: str, end: str) -> int:
    """含首尾的天数；解析失败时退回 2 天。"""
    try:
        s = date.fromisoformat(start)
        e = date.fromisoformat(end)
        return max((e - s).days + 1, 1)
    except (ValueError, TypeError):
        return 2


def build_sample_plan(inputs: dict) -> dict:
    """根据输入合成一份「结构完整、预算自洽、类别覆盖偏好」的计划。

    用于在没有大模型 / LangSmith 的情况下端到端演示评估流水线（三项得分应≈1.0）。
    把它替换成真实的 graph.ainvoke 包装即可评估真实输出。
    """
    start = inputs.get("start_date", "2026-06-01")
    end = inputs.get("end_date", "2026-06-02")
    n_days = _days_between(start, end)
    prefs = inputs.get("preferences") or ["综合"]
    base = date.fromisoformat(start) if _valid(start) else date(2026, 6, 1)

    # 每日市内交通费随交通方式而变：打车/网约车明显高于公交地铁、步行骑行近乎免费。
    per_day_transport = _per_day_transport_cost(inputs.get("transport") or [])

    days = []
    attractions_total = hotels_total = meals_total = transport_total = 0.0
    for i in range(n_days):
        d = (base + timedelta(days=i)).isoformat()
        # 每个偏好造一个对应类别的景点，确保 preference_match 命中。
        # 附带三亚附近的 GCJ-02 坐标（按天/序号轻微散开），方便离线演示地图打点。
        attractions = [
            {
                "name": f"{p}景点{i + 1}",
                "category": p,
                "ticket_price": 50.0,
                "visit_duration": 120,
                "location": {
                    "longitude": round(109.51 + i * 0.012 + j * 0.006, 6),
                    "latitude": round(18.25 + i * 0.009 + j * 0.004, 6),
                },
            }
            for j, p in enumerate(prefs)
        ]
        meals = [
            {"type": "breakfast", "name": "早餐", "estimated_cost": 30.0},
            {"type": "lunch", "name": "午餐", "estimated_cost": 60.0},
            {"type": "dinner", "name": "晚餐", "estimated_cost": 80.0},
        ]
        hotel = {
            "name": f"{inputs.get('city', '')}酒店",
            "estimated_cost": 400.0,
            "location": {
                "longitude": round(109.50 + i * 0.012, 6),
                "latitude": round(18.24 + i * 0.009, 6),
            },
        }
        days.append(
            {
                "date": d,
                "day_index": i,
                "transportation": "、".join(inputs.get("transport") or []),
                "hotel": hotel,
                "attractions": attractions,
                "meals": meals,
            }
        )
        attractions_total += sum(a["ticket_price"] for a in attractions)
        hotels_total += hotel["estimated_cost"]
        meals_total += sum(m["estimated_cost"] for m in meals)
        transport_total += per_day_transport

    budget = {
        "total_attractions": attractions_total,
        "total_hotels": hotels_total,
        "total_meals": meals_total,
        "total_transportation": transport_total,
        "total": attractions_total + hotels_total + meals_total + transport_total,
    }
    return {
        "city": inputs.get("city", ""),
        "start_date": start,
        "end_date": end,
        "preferences": prefs,
        "days": days,
        "budget": budget,
        "weather_info": [],
        "overall_suggestions": "",
    }


def _per_day_transport_cost(transport: list[str]) -> float:
    """按交通方式估算每天市内交通费（元/天）。

    打车/网约车单程就有起步价 + 里程费，一天多段往往上百元，明显高于公交地铁；
    步行/骑行接近零。让合成预算与「所选交通方式」自洽，避免选打车却只估几十元。
    """
    joined = "".join(transport)
    if any(k in joined for k in ("打车", "网约车", "出租", "taxi")):
        return 120.0
    if "自驾" in joined:
        return 100.0
    if any(k in joined for k in ("地铁", "公交", "公共交通", "缆车")):
        return 25.0
    return 10.0  # 步行 / 骑行 / 未指定


def _valid(d: str) -> bool:
    try:
        date.fromisoformat(d)
        return True
    except (ValueError, TypeError):
        return False


# ==================== 本地评估运行 ====================

def run_local_eval(planner: Callable[[dict], dict] = build_sample_plan) -> list[dict]:
    """对 TEST_CASES 逐例生成计划并打分，返回每例的 {name, scores} 列表并打印汇总。"""
    rows = []
    for case in TEST_CASES:
        plan = planner(case["inputs"])
        scores = evaluate_plan(plan, case["expected"])
        rows.append({"name": case["name"], "scores": scores})

    print("\n=== 本地评估（合成计划） ===")
    keys = ["completeness", "preference_match", "budget_consistency"]
    print(f"{'case':<12} " + "  ".join(f"{k:<18}" for k in keys))
    for row in rows:
        cells = "  ".join(f"{row['scores'][k]:<18.2f}" for k in keys)
        print(f"{row['name']:<12} {cells}")

    # 各指标的均值。
    print("-" * 70)
    avg = {k: sum(r["scores"][k] for r in rows) / len(rows) for k in keys}
    print(f"{'AVG':<12} " + "  ".join(f"{avg[k]:<18.2f}" for k in keys))
    return rows


# ==================== LangSmith 适配（可选） ====================

def make_langsmith_evaluators() -> list[Callable]:
    """把三个评估器包装成 langsmith.evaluate(evaluators=[...]) 接受的 (run, example) 形态。

    约定 run.outputs 即计划 dict（或含 final_plan），example.outputs 为期望 dict。
    """
    def _wrap(fn):
        def _ls(run, example):
            outputs = getattr(run, "outputs", None) or {}
            plan = outputs.get("final_plan") or outputs
            expected = getattr(example, "outputs", None) if example is not None else None
            return fn(plan, expected)

        _ls.__name__ = fn.__name__
        return _ls

    return [_wrap(fn) for fn in EVALUATORS]


def run_langsmith_eval(dataset_name: str = "travel-agent-eval"):  # pragma: no cover
    """把 TEST_CASES 推送为 LangSmith 数据集并对 build_sample_plan 评估（需 LANGCHAIN_API_KEY）。

    仅在显式调用且配置了 key 时运行；CI / 本地默认走 run_local_eval。
    """
    import os

    if not os.getenv("LANGCHAIN_API_KEY"):
        raise RuntimeError("未配置 LANGCHAIN_API_KEY；请改用 run_local_eval()。")

    from langsmith import Client
    from langsmith.evaluation import evaluate

    client = Client()
    if not client.has_dataset(dataset_name=dataset_name):
        ds = client.create_dataset(dataset_name=dataset_name)
        client.create_examples(
            inputs=[c["inputs"] for c in TEST_CASES],
            outputs=[c["expected"] for c in TEST_CASES],
            dataset_id=ds.id,
        )

    return evaluate(
        lambda inputs: {"final_plan": build_sample_plan(inputs)},
        data=dataset_name,
        evaluators=make_langsmith_evaluators(),
    )


if __name__ == "__main__":
    run_local_eval()
