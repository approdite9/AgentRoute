"""
智能旅行助手 —— 入口。

用法:
    python Agent.py                # 流式进度 + 最终渲染（默认）
    python Agent.py --no-stream    # 仅非流式输出
"""
import asyncio
import json
import sys

from logging_config import configure_logging

# CLI 入口：尽早配置 JSON 日志（输出到 stderr，不污染 stdout 的行程渲染）。
configure_logging()

from agents.planner import TripPlanner
from render import format_plan_cli


# 节点 → 控制台进度标签
NODE_LABELS = {
    "weather":    "🌤️  查询天气...",
    "poi":        "🏛️  搜索景点...",
    "hotel":      "🏨  搜索酒店...",
    "route":      "🚌  规划路线...",
    "synthesize": "🧩  整合行程...",
}


def _render(plan: dict):
    """把行程 dict 渲染为 CLI 文本。"""
    formatted = format_plan_cli(json.dumps(plan, ensure_ascii=False))
    if formatted:
        print(formatted)
    else:
        print(plan or "（未生成有效行程）")


# ==================== 演示 ====================

async def demo_stream(planner: TripPlanner, user_input: str):
    """流式输出演示 —— 实时打印节点进度，结束后渲染最终行程。"""
    print("=" * 60)
    print(f"🚀 正在为您规划旅行...\n输入: {user_input}\n")
    print("=" * 60)

    final_plan: dict = {}
    seen: set[str] = set()

    async for event in planner.stream(user_input):
        kind = event.get("event", "")
        name = event.get("name", "")

        if kind == "on_chain_start" and name in NODE_LABELS and name not in seen:
            seen.add(name)
            print(NODE_LABELS[name], flush=True)

        if kind == "on_chain_end" and name == "synthesize":
            output = (event.get("data") or {}).get("output")
            if isinstance(output, dict) and output.get("final_plan"):
                final_plan = output["final_plan"]

    print("=" * 60)
    _render(final_plan)
    print("=" * 60)
    print("✅ 旅行计划生成完毕")


async def demo_invoke(planner: TripPlanner, user_input: str):
    """非流式输出演示。"""
    print("=" * 60)
    print(f"🚀 正在为您规划旅行...\n输入: {user_input}\n")
    print("=" * 60)

    plan = await planner.invoke(user_input)
    _render(plan)

    print("=" * 60)
    print("✅ 旅行计划生成完毕")


async def main():
    planner = TripPlanner()

    user_input = "长沙3日游，2026年6月21日-2026年6月23日，喜欢自然风光和历史文化，中等预算，住五一广场"

    if "--no-stream" in sys.argv:
        await demo_invoke(planner, user_input)
    else:
        await demo_stream(planner, user_input)


if __name__ == "__main__":
    asyncio.run(main())
