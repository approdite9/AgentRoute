"""OpenTelemetry 接入（A5）—— 可选分布式追踪，无依赖时降级为 no-op。

行业在把可观测性收敛到 OpenTelemetry 标准（与 n8n 2.37 的 OpenTelemetry 同方向）。
本模块把「是否真正启用 OTel」与「业务代码怎么打 span」解耦：

- 未装 opentelemetry-sdk / 未设 OTEL_EXPORTER_OTLP_ENDPOINT：`init_tracing()` 返回 False，
  `span()` 变成零开销的 no-op 上下文管理器，业务代码无需改动、CI 无新依赖。
- 装了并配置了 endpoint：初始化 TracerProvider + OTLP exporter，`span()` 产出真实 span。

用法：
    from monitoring.otel import init_tracing, span
    init_tracing("agentroute-api")      # 进程启动时调一次（幂等）
    with span("plan_trip", city="成都"):
        ...
"""
from __future__ import annotations

import contextlib
import os

import structlog

logger = structlog.get_logger(__name__)

_TRACER = None          # 真实 tracer（启用时）；None 表示 no-op
_INITIALIZED = False


def _otel_enabled() -> bool:
    """是否应启用真实 OTel：需显式配置 OTLP endpoint（避免默认就尝试连不存在的 collector）。"""
    return bool(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))


def init_tracing(service_name: str = "agentroute") -> bool:
    """初始化 OTel 追踪（幂等）。返回是否真正启用了真实 tracer。

    无 opentelemetry 依赖 / 未配 endpoint 时返回 False，后续 span() 走 no-op。
    """
    global _TRACER, _INITIALIZED
    if _INITIALIZED:
        return _TRACER is not None
    _INITIALIZED = True

    if not _otel_enabled():
        logger.info("otel_disabled", reason="OTEL_EXPORTER_OTLP_ENDPOINT 未设置，使用 no-op")
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
        _TRACER = trace.get_tracer(service_name)
        logger.info("otel_enabled", service=service_name,
                    endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))
        return True
    except Exception as exc:  # noqa: BLE001 —— 依赖缺失/初始化失败一律降级 no-op，不阻断启动
        logger.warning("otel_init_failed_fallback_noop", error=str(exc))
        _TRACER = None
        return False


@contextlib.contextmanager
def span(name: str, **attributes):
    """打一个 span 的上下文管理器。真实 tracer 时产出 span，否则零开销 no-op。

    异常会被记录到 span（若启用）后重新抛出，不吞异常。
    """
    if _TRACER is None:
        yield None
        return
    with _TRACER.start_as_current_span(name) as sp:
        try:
            for k, v in attributes.items():
                sp.set_attribute(k, v)
        except Exception:  # noqa: BLE001 —— 属性设置失败不影响业务
            pass
        yield sp


def is_tracing_enabled() -> bool:
    """当前是否启用了真实 OTel tracer（供健康检查/诊断使用）。"""
    return _TRACER is not None
