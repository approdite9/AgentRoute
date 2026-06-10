"""沙箱 + Skill 注册表测试（同步、隔离子进程）。

注：被沙箱执行的目标函数定义在模块顶层，fork 子进程直接继承，无需 pickle/import。
"""
import os
import time

import pytest

import sandbox
import skills
from sandbox import SandboxError, SandboxLimits, SandboxTimeout, run_in_sandbox, sandboxed


# ---- 沙箱目标函数（模块级，供子进程执行）----
def _add(a, b):
    return a + b


def _raise():
    raise ValueError("boom in sandbox")


def _sleep_long():
    time.sleep(3)
    return "done"


def _hard_exit():
    os._exit(7)  # 模拟被资源上限/段错误杀掉：不回传结果


@sandboxed(SandboxLimits(timeout_s=2))
def _decorated_mul(a, b):
    return a * b


# ==================== 沙箱 ====================

def test_sandbox_normal_return():
    assert run_in_sandbox(_add, (2, 3)) == 5


def test_sandbox_captures_exception():
    with pytest.raises(SandboxError) as ei:
        run_in_sandbox(_raise)
    assert "ValueError" in str(ei.value)


def test_sandbox_timeout_kills_process():
    with pytest.raises(SandboxTimeout):
        run_in_sandbox(_sleep_long, limits=SandboxLimits(timeout_s=0.5))


def test_sandbox_abnormal_exit_is_error():
    """子进程异常退出（无回传）→ SandboxError（覆盖 OOM/SIGXCPU/段错误等）。"""
    with pytest.raises(SandboxError):
        run_in_sandbox(_hard_exit)


def test_sandboxed_decorator():
    assert _decorated_mul(4, 5) == 20
    assert getattr(_decorated_mul, "sandboxed", False) is True


# ==================== Skill 注册表 ====================

def test_registry_register_get_list():
    reg = skills.SkillRegistry()
    reg.register(skills.Skill("a", "desc", "test", handler=lambda: 1))
    assert "a" in reg.list()
    assert reg.get("a").description == "desc"


def test_registry_duplicate_and_unknown():
    reg = skills.SkillRegistry()
    reg.register(skills.Skill("a", "d", "t", handler=lambda: 1))
    with pytest.raises(ValueError):
        reg.register(skills.Skill("a", "d2", "t"))      # 重复
    with pytest.raises(KeyError):
        reg.get("missing")                               # 未注册


def test_registry_invoke_and_declaration_only():
    reg = skills.SkillRegistry()
    reg.register(skills.Skill("inc", "d", "t", handler=lambda x: x + 1))
    assert reg.invoke("inc", 41) == 42
    reg.register(skills.Skill("decl", "d", "t", handler=None))  # 仅声明
    with pytest.raises(ValueError):
        reg.invoke("decl")


def test_builtin_skills_catalog_and_tools():
    """内置 catalog 含五个领域，且工具集来自 config.tool_domains。"""
    listed = skills.REGISTRY.list()
    for name in ["weather", "poi", "hotel", "route", "rag"]:
        assert name in listed
    poi = skills.REGISTRY.get("poi")
    assert "maps_text_search" in poi.tools          # 来自 config.tool_domains
    assert poi.handler is None                       # catalog 条目仅声明


def test_sandboxed_skill_runs_in_sandbox():
    """沙箱化 skill 经 invoke 在子进程隔离执行并返回正确结果。"""
    out = skills.REGISTRY.invoke("compute_budget_total", {"景点": 180, "酒店": 1200, "餐饮": 480})
    assert out == 1860.0
    assert skills.REGISTRY.get("compute_budget_total").sandboxed is True
