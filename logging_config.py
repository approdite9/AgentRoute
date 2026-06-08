"""
全局可观测性初始化 —— structlog（JSON 日志）+ LangSmith 追踪环境变量。

在每个进程入口处尽早调用 configure_logging()：
  - api/main.py        （FastAPI 进程）
  - tasks/celery_app.py（Celery worker 进程）
  - Agent.py           （CLI 入口）

日志输出到 **stderr** 而非 stdout：CLI（Agent.py）的 stdout 用来打印渲染好的
行程文本，把结构化日志分流到 stderr，二者互不污染（也契合用 `2>` 抓日志的习惯）。
"""
import logging
import os
import sys

import structlog

from config import settings


def configure_logging() -> None:
    """配置 structlog 全局输出为机器可解析的 JSON；并就绪 LangSmith 追踪环境。"""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.JSONRenderer(),  # 机器可解析的 JSON 输出
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    # ===== LangSmith 分布式追踪（TASK C）=====
    # LangChain 在运行时读取这些环境变量，故在此设置（晚于 import 也生效）。
    # 仅在确实配置了 API Key 时才打开追踪：否则 LangChain 会向后台反复发送追踪
    # 请求并失败、刷屏 warning，反而污染日志。无 key 时显式关掉更干净。
    tracing_on = settings.langchain_tracing_v2 and bool(settings.langchain_api_key)
    os.environ["LANGCHAIN_TRACING_V2"] = str(tracing_on).lower()
    os.environ["LANGCHAIN_PROJECT"] = settings.langchain_project
    if settings.langchain_api_key:
        os.environ["LANGCHAIN_API_KEY"] = settings.langchain_api_key

    # Prometheus 多进程目录若已设置，确保其存在（真正的 export 在 shell 启动命令里，
    # 必须早于首次 import prometheus_client；此处仅作兜底建目录）。
    multiproc_dir = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if multiproc_dir:
        os.makedirs(multiproc_dir, exist_ok=True)
