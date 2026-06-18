"""
Celery 应用配置 —— broker / result backend / 队列路由 / 超时限制。

Redis DB 映射（与 project_planning.md 第 6 节一致）：
  DB 0  Celery broker（任务队列）
  DB 1  Celery result backend（任务结果）
  DB 5  Pub/Sub 流式 token（见 tasks.trip_tasks）

settings.redis_url 形如 "redis://localhost:6379"（不带 db 后缀），
这里按用途分别拼出 /0、/1。
"""
import os
import sys

# 把项目根目录钉到 sys.path：Celery 以 import_from_cwd 加载 -A 目标时，只在启动阶段
# 临时把 cwd 放进 sys.path，启动后即移除；若 worker 恰好从项目根启动（cwd == _ROOT），
# 用「if _ROOT not in sys.path」判断会被这条临时项骗过而跳过，随后临时项被移除，_ROOT 反而
# 落不到 path 上。故这里无条件 append 一份留在末尾：import_from_cwd 的 remove() 只摘掉它
# 自己插在 index 0 的那条，末尾这份得以幸存。（trip_tasks 在模块顶层导入 agents.graph，
# 真正保证了 agents.* 在启动窗口内进入 sys.modules，本条仅作纵深防御。）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(_ROOT)

from celery import Celery

from config import settings
from logging_config import configure_logging

# worker 进程启动即配置 JSON 日志 + LangSmith 追踪环境（在任何任务运行之前）。
configure_logging()

# settings.redis_url 末尾可能带或不带 "/"，统一去掉后再拼 db 编号。
_redis_base = settings.redis_url.rstrip("/")

celery_app = Celery("travel_agent")
celery_app.conf.update(
    broker_url=f"{_redis_base}/0",
    result_backend=f"{_redis_base}/1",
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,          # 让 AsyncResult 能区分 PENDING / STARTED
    # 推理模型（deepseek-r1）单步就要数十秒，4 个子 Agent + RAG + 整合叠加可达数分钟，
    # 故放宽超时；换指令模型（qwen3-max/deepseek-v3）可调回更紧的值。
    task_time_limit=660,              # 11 min 硬超时（SIGKILL）
    task_soft_time_limit=600,         # 10 min 软超时（抛 SoftTimeLimitExceeded）
    result_expires=3600,             # 结果在 backend 中保留 1h
    # 不做自定义队列路由：本项目只有 plan_trip 一个 Celery 任务（weather/poi 等是
    # LangGraph 图内节点，并非独立 Celery 任务）。此前把任务路由到 trip.planning，
    # 一旦 worker 启动命令漏了 `-Q trip.planning` 就永远取不到任务、前端卡在「开始规划」。
    # 改走默认队列（celery），任何 `celery -A tasks.celery_app worker` 都能消费，杜绝该坑。
)

# 确保 worker 启动时任务模块被导入并注册到该 app。
celery_app.autodiscover_tasks(["tasks"])

# 显式导入，保证 `celery -A tasks.celery_app worker` 能发现任务（即便 autodiscover
# 因包结构未命中也不影响）。放在文件末尾避免循环导入。
from tasks import trip_tasks  # noqa: E402,F401
