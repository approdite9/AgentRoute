"""
Prometheus 指标定义 —— 行程规划链路的吞吐、时延、外部调用、缓存命中。

多进程聚合（关键）：
  /metrics 由 FastAPI 进程暴露，但 TRIPS_COMPLETED / PLANNING_DURATION /
  NODE_DURATION / MCP_CALLS / CACHE_HITS / ACTIVE_TASKS 等大多在 **Celery worker
  进程**里自增。prometheus_client 的 Counter/Gauge 默认是「进程内」的，worker 自增
  的值不会出现在 API 的 /metrics 上。

  解决办法是 prometheus_client 的「多进程模式」：进程启动前导出环境变量
      PROMETHEUS_MULTIPROC_DIR=<可写空目录>
  之后每个进程把指标写进该目录的内存映射文件；API 的 /metrics 用
  MultiProcessCollector 把所有进程的文件聚合后再渲染（见 render_latest）。
  uvicorn 与 celery worker 必须设同一个目录；该目录应在每次重启前清空，
  否则上一轮的计数会残留累加。未设该环境变量时退化为单进程模式（CLI / 测试足够用）。

  注意：该环境变量必须在「首次 import prometheus_client 之前」就存在于环境里
  （prometheus_client 在导入时即决定用单进程还是多进程的值存储），因此应在
  shell 启动命令里 export，而不是在 Python 里再设置。
"""
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

import os

# ==================== 业务指标 ====================

TRIPS_SUBMITTED = Counter(
    "trips_submitted_total", "Trip planning requests submitted"
)
TRIPS_COMPLETED = Counter(
    "trips_completed_total", "Trip plans completed", ["status"]
)  # status: done|error

PLANNING_DURATION = Histogram(
    "planning_duration_seconds",
    "Total planning wall time",
    buckets=[10, 30, 60, 90, 120, 180],
)

# 单节点动辄十几到数十秒（synthesize 可达 ~30s），故用面向「秒级」的桶，
# 否则全部落进默认桶的 +Inf 失去分辨率。
NODE_DURATION = Histogram(
    "node_duration_seconds",
    "Per-node execution time",
    ["node"],
    buckets=[0.5, 1, 2, 5, 10, 20, 30, 60, 120],
)

MCP_CALLS = Counter(
    "mcp_calls_total", "MCP tool invocations", ["tool", "status"]
)  # status: success|error
CACHE_HITS = Counter("cache_hits_total", "Redis cache hits", ["cache"])
CACHE_MISSES = Counter("cache_misses_total", "Redis cache misses", ["cache"])

# Gauge 在多进程模式下必须指定聚合方式；livesum = 所有「存活」进程之和。
ACTIVE_TASKS = Gauge(
    "celery_active_tasks",
    "Currently executing Celery tasks",
    multiprocess_mode="livesum",
)


# ==================== 暴露 ====================

def render_latest() -> tuple[bytes, str]:
    """渲染 /metrics 文本。多进程模式下聚合所有进程的指标文件。"""
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        # 延迟导入：仅多进程模式需要，且依赖上面的环境变量已就位。
        from prometheus_client import multiprocess

        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        return generate_latest(registry), CONTENT_TYPE_LATEST
    return generate_latest(), CONTENT_TYPE_LATEST
