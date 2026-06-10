"""轻量 Skill 注册表 —— 把能力声明式登记（名称/描述/领域/可用工具/是否沙箱），
统一发现(list/describe)与调用(invoke)，沙箱化 skill 经 sandbox 隔离执行。

定位：① 各领域子 Agent（weather/poi/hotel/route/rag）登记为 catalog（元数据 + 来自
config.tool_domains 的最小可用工具集），便于发现、观测、按指标治理；② 重计算/不可信
能力登记为 sandboxed skill，调用即在子进程隔离 + 超时 + 资源上限下执行。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from sandbox import SandboxLimits, run_in_sandbox


@dataclass
class Skill:
    name: str
    description: str
    domain: str
    handler: Callable[..., Any] | None = None  # None = 仅声明的 catalog 条目
    sandboxed: bool = False
    limits: SandboxLimits | None = None
    tools: list[str] = field(default_factory=list)  # 该 skill 允许的 MCP 工具（最小集）


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill, *, overwrite: bool = False) -> Skill:
        if skill.name in self._skills and not overwrite:
            raise ValueError(f"skill 已注册: {skill.name}")
        self._skills[skill.name] = skill
        return skill

    def get(self, name: str) -> Skill:
        if name not in self._skills:
            raise KeyError(f"未注册的 skill: {name}")
        return self._skills[name]

    def list(self) -> list[str]:
        return sorted(self._skills)

    def describe(self) -> list[dict]:
        return [
            {
                "name": s.name, "domain": s.domain, "description": s.description,
                "sandboxed": s.sandboxed, "tools": s.tools, "callable": s.handler is not None,
            }
            for s in (self._skills[n] for n in self.list())
        ]

    def invoke(self, name: str, *args, **kwargs) -> Any:
        s = self.get(name)
        if s.handler is None:
            raise ValueError(f"skill '{name}' 仅声明、无可执行 handler")
        if s.sandboxed:
            return run_in_sandbox(s.handler, args, kwargs, s.limits)
        return s.handler(*args, **kwargs)


REGISTRY = SkillRegistry()


def skill(name, description, domain, *, sandboxed=False, limits=None, tools=None):
    """装饰器：登记一个可执行 skill。"""
    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        REGISTRY.register(
            Skill(name, description, domain, fn, sandboxed, limits, list(tools or []))
        )
        return fn
    return deco


def register_builtin_skills(registry: SkillRegistry = REGISTRY) -> None:
    """登记四个领域子 Agent + RAG 为 catalog（元数据 + 可用工具来自 config.tool_domains）。"""
    try:
        from config import settings
        td = settings.tool_domains
    except Exception:  # noqa: BLE001 —— 取不到配置也不影响注册表可用
        td = {}
    catalog = [
        ("weather", "查询目的地天气", td.get("weather", [])),
        ("poi", "按用户偏好搜索景点", td.get("poi", [])),
        ("hotel", "按住宿偏好搜索酒店", td.get("hotel", [])),
        ("route", "规划相邻景点间交通路线（按交通方式估费）", td.get("route", [])),
        ("rag", "检索旅行知识(攻略/口碑/玩法)增强内容", []),
    ]
    for name, desc, tools in catalog:
        if name not in registry._skills:
            registry.register(Skill(name, desc, domain=name, handler=None, tools=list(tools)))


# ---- 示例：一个真正在沙箱内执行的纯计算 skill（演示沙箱链路落地）----
@skill(
    "compute_budget_total",
    "把预算各分项求和（示例：在子进程沙箱内执行的纯计算 skill）",
    domain="compute",
    sandboxed=True,
    limits=SandboxLimits(timeout_s=5, memory_mb=256),
)
def compute_budget_total(components: dict) -> float:
    return float(sum(components.values()))


register_builtin_skills()
