"""执行沙箱 —— 子进程隔离 + 超时 + 资源上限(RLIMIT)。

用途：隔离"不可信 / 重计算 / 可能挂死"的工具或代码执行，把崩溃、死循环、内存泄漏的
爆炸半径限制在一个子进程里，不波及主服务。MCP 网络工具走的是轻隔离（超时 + 递归上限），
本模块提供的是**真正的进程级隔离**（独立地址空间 + RLIMIT_AS/CPU + 超时强杀）。

注：用 fork 上下文（Unix），故被执行函数无需可 pickle；结果经 Pipe 回传需可 pickle。
"""
from __future__ import annotations

import multiprocessing as mp
from dataclasses import dataclass
from functools import wraps
from typing import Any, Callable


class SandboxError(Exception):
    """沙箱内执行失败（子进程抛异常 / 异常退出 / 超资源被杀）。"""


class SandboxTimeout(SandboxError):
    """沙箱执行超时。"""


@dataclass
class SandboxLimits:
    timeout_s: float = 10.0          # 墙钟超时，超时强杀子进程
    memory_mb: int | None = 512      # 地址空间上限 RLIMIT_AS（Linux 强制；macOS 可能放宽）
    cpu_s: int | None = None         # CPU 时间上限 RLIMIT_CPU（超时发 SIGXCPU）


def _apply_limits(limits: SandboxLimits) -> None:
    try:
        import resource
    except ImportError:  # 非 Unix
        return
    if limits.memory_mb:
        n = limits.memory_mb * 1024 * 1024
        for res in ("RLIMIT_AS", "RLIMIT_DATA"):
            try:
                resource.setrlimit(getattr(resource, res), (n, n))
            except (ValueError, OSError, AttributeError):
                pass
    if limits.cpu_s:
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_s, limits.cpu_s))
        except (ValueError, OSError):
            pass


def _child(func, args, kwargs, limits, conn) -> None:
    try:
        _apply_limits(limits)
        result = func(*args, **(kwargs or {}))
        conn.send(("ok", result))
    except MemoryError:
        conn.send(("err", "MemoryError: 超出内存上限"))
    except Exception as exc:  # noqa: BLE001 —— 把子进程异常带回父进程
        conn.send(("err", f"{type(exc).__name__}: {exc}"))
    finally:
        conn.close()


def _ctx():
    # fork：被执行函数无需可 pickle、可跑闭包；独立进程仍提供隔离 + RLIMIT + 强杀。
    try:
        return mp.get_context("fork")
    except ValueError:  # 平台不支持 fork（如 Windows）→ spawn
        return mp.get_context("spawn")


def run_in_sandbox(
    func: Callable[..., Any],
    args: tuple = (),
    kwargs: dict | None = None,
    limits: SandboxLimits | None = None,
) -> Any:
    """在隔离子进程里执行 func，受 limits 约束。成功返回结果；否则抛 Sandbox(Timeout)Error。"""
    limits = limits or SandboxLimits()
    ctx = _ctx()
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_child, args=(func, args, kwargs, limits, child_conn))
    proc.start()
    child_conn.close()  # 父进程不写端
    proc.join(limits.timeout_s)

    if proc.is_alive():
        proc.terminate()
        proc.join()
        raise SandboxTimeout(f"sandboxed call exceeded {limits.timeout_s}s")

    if parent_conn.poll():
        try:
            status, payload = parent_conn.recv()
        except EOFError:
            status = None  # 管道在 EOF（子进程没写就退了）→ 当作异常退出处理
        else:
            if status == "ok":
                return payload
            raise SandboxError(payload)

    # 没有任何有效回传：子进程异常退出（被 OOM/SIGXCPU/段错误等杀掉）。
    raise SandboxError(f"sandboxed process exited abnormally (rc={proc.exitcode}); 可能超出资源上限")


def sandboxed(limits: SandboxLimits | None = None):
    """装饰器：把一个函数的每次调用都放进沙箱执行。"""
    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(fn)
        def wrapper(*args, **kwargs):
            return run_in_sandbox(fn, args, kwargs, limits)
        wrapper.sandboxed = True  # type: ignore[attr-defined]
        return wrapper
    return deco
