"""
Celery 行程规划任务 —— 在 worker 进程里跑 LangGraph 图，并把流式 token
通过 Redis pub/sub（频道 stream:{task_id}）推给 FastAPI 的 SSE 端点。
同时把状态机与节点级审计写入 PostgreSQL（trip_plans / audit_logs）。

设计要点：
  - 同步任务体内用「新建事件循环 + run_until_complete」驱动异步图，
    每个任务一个干净的 loop（cache.CacheClient 会自动按 loop 重建连接池）。
  - DB 会话用 db.session.worker_session()：每任务现建 NullPool 引擎，杜绝跨 loop
    复用连接池（asyncpg 连接绑定在创建它的 loop 上）。
  - 发布器（sync redis，db=5）按 worker 进程懒加载一次并复用，避免每个 token
    都新建一次连接。redis-py 的连接池是线程安全的。
  - 频道消息协议：
      {"type": "token",      "content": str}
      {"type": "tool_start", "name": str}
      {"type": "tool_end",   "name": str}
      {"type": "done",       "plan": dict}
      {"type": "error",      "message": str}
  - 状态机：pending（由 API 入库）→ planning（任务启动）→ done / error（任务结束）。
  - 审计：每个图节点（weather/poi/hotel/route/synthesize/error_handler）完成写一条
    AuditLog(event="node_complete", detail={node_name, duration_ms})。
"""
import asyncio
import contextlib
import json
import time
import uuid

import redis as sync_redis
import structlog
from sqlalchemy import update

from agents.graph import build_graph
from config import settings
from db.models import AuditLog, TripPlan
from db.session import worker_session
from monitoring.metrics import ACTIVE_TASKS, PLANNING_DURATION, TRIPS_COMPLETED
from tasks.celery_app import celery_app

logger = structlog.get_logger(__name__)

# 图中的节点名（与 agents/graph.py 的 add_node 一致）——用于从 astream_events
# 的 on_chain_start/end 事件里筛出「节点级」事件，忽略图本身/内部 runnable 的链事件。
_NODE_NAMES = {"weather", "poi", "hotel", "route", "synthesize", "error_handler"}

# 进程级单例发布器：worker 是同步多进程模型，每个进程建一个 db=5 的客户端复用。
_publisher: sync_redis.Redis | None = None

# 检查点表是否已初始化（进程级，避免每个任务都跑一次建表/迁移）。
_checkpoint_ready = False


@contextlib.asynccontextmanager
async def _graph_with_checkpointer():
    """产出一张「带持久化检查点」的已编译图。

    正式版用 AsyncPostgresSaver（复用业务 Postgres，标准表、无需 Redis Stack 模块），
    让检查点跨 worker 重启存活、可被多轮修改/人审断点恢复读取。
    未配置 DATABASE_URL 或后端不可用时，降级为内存检查点（MemorySaver），保证规划不被阻断。

    检查点必须在「与 astream_events/aget_state 同一个事件循环」内创建与使用，故每个任务
    在自己的 loop 里新建 saver（与 worker_session 的 per-task 引擎一致）。
    """
    global _checkpoint_ready
    db_url = settings.checkpoint_db_url()
    if db_url:
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            async with AsyncPostgresSaver.from_conn_string(db_url) as saver:
                if not _checkpoint_ready:
                    try:
                        await saver.setup()  # 幂等：首次建检查点表/跑迁移，之后近似 no-op
                    except Exception as exc:  # noqa: BLE001 —— 并发首建可能撞车，忽略
                        logger.warning("checkpoint_setup_failed", error=str(exc))
                    _checkpoint_ready = True
                yield build_graph(checkpointer=saver)
                return
        except Exception as exc:  # noqa: BLE001 —— DB 不可用 → 降级内存检查点，不阻断规划
            logger.warning("checkpoint_postgres_unavailable", error=str(exc))
    yield build_graph()


def _get_publisher() -> sync_redis.Redis:
    global _publisher
    if _publisher is None:
        _publisher = sync_redis.Redis.from_url(
            settings.redis_url, db=5, decode_responses=True
        )
    return _publisher


def _publish_to_channel(channel: str, data: dict) -> None:
    """向 stream:{task_id} 频道发布一条 JSON 消息（pub/sub，无持久化）。"""
    try:
        _get_publisher().publish(channel, json.dumps(data, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 —— 发布失败不应让整个任务崩掉
        logger.warning("publish_failed", channel=channel, error=str(exc))


def _extract_usage(output) -> dict | None:
    """从 on_chat_model_end 的 output 里尽力取 token 用量（ChatTongyi 流式下可能缺）。"""
    usage = getattr(output, "usage_metadata", None)
    if isinstance(usage, dict) and usage.get("total_tokens"):
        return usage
    return None


async def _set_trip_status(session, trip_uuid: uuid.UUID | None, **fields) -> None:
    """更新 trip_plans 一行的若干列并提交（updated_at 由 onupdate 自动刷新）。"""
    if trip_uuid is None:
        return
    await session.execute(
        update(TripPlan).where(TripPlan.id == trip_uuid).values(**fields)
    )
    await session.commit()


async def _add_audit(session, trip_uuid: uuid.UUID | None, event: str, detail: dict) -> None:
    """追加一条审计日志。审计是「尽力而为」：写失败只告警，绝不拖垮规划本身。"""
    if trip_uuid is None:
        return
    try:
        session.add(AuditLog(trip_id=trip_uuid, event=event, detail=detail))
        await session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("audit_write_failed", event=event, error=str(exc))
        await session.rollback()


@celery_app.task(bind=True, name="tasks.trip_tasks.plan_trip")
def plan_trip_task(self, state_dict: dict, trip_id: str | None = None) -> dict:
    """
    长耗时规划任务。运行 LangGraph 图，逐 token 推流到 Redis pub/sub（频道
    stream:{task_id}），并把状态机/节点审计写入 PostgreSQL。返回最终 plan dict。

    trip_id 为 API 入库时生成的 trip_plans 主键（字符串）；为空时退化为「无持久化」模式，
    任务仍可正常规划（便于单测或不带 DB 的调用）。
    """
    task_id = self.request.id
    channel = f"stream:{task_id}"
    trip_uuid = uuid.UUID(trip_id) if trip_id else None
    logger.info("plan_trip_start", task_id=task_id, trip_id=trip_id, city=state_dict.get("city"))

    async def _run() -> dict:
        # 持久化检查点（AsyncPostgresSaver，跨重启存活）；不可用时降级内存检查点。
        # thread_id 按 trip 稳定，使多轮修改 / 人审断点能定位到同一会话的检查点。
        async with _graph_with_checkpointer() as graph:
            config = {"configurable": {"thread_id": f"celery-{trip_id or task_id}"}}

            async with worker_session() as session:
                try:
                    await _set_trip_status(session, trip_uuid, status="planning")

                    node_starts: dict[str, float] = {}
                    usage_total = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

                    async for event in graph.astream_events(state_dict, config=config, version="v2"):
                        kind = event.get("event", "")
                        if kind == "on_chat_model_stream":
                            chunk = event["data"].get("chunk")
                            token = getattr(chunk, "content", "") if chunk is not None else ""
                            if token:
                                _publish_to_channel(channel, {"type": "token", "content": token})
                        elif kind == "on_chat_model_end":
                            usage = _extract_usage(event["data"].get("output"))
                            if usage:
                                for k in usage_total:
                                    usage_total[k] += usage.get(k, 0) or 0
                        elif kind == "on_tool_start":
                            _publish_to_channel(channel, {"type": "tool_start", "name": event.get("name")})
                        elif kind == "on_tool_end":
                            _publish_to_channel(channel, {"type": "tool_end", "name": event.get("name")})
                        elif kind == "on_chain_start" and event.get("name") in _NODE_NAMES:
                            node_starts[event["run_id"]] = time.perf_counter()
                        elif kind == "on_chain_end" and event.get("name") in _NODE_NAMES:
                            started = node_starts.pop(event["run_id"], None)
                            duration_ms = int((time.perf_counter() - started) * 1000) if started else None
                            await _add_audit(
                                session,
                                trip_uuid,
                                "node_complete",
                                {"node_name": event["name"], "duration_ms": duration_ms},
                            )

                    # 取最终状态：final_plan（synthesis_node 写入）+ error（error_node 写入）。
                    final = await graph.aget_state(config)
                    plan = final.values.get("final_plan")
                    error = final.values.get("error")
                    token_usage = usage_total if usage_total["total_tokens"] else None

                    if not plan:
                        # 图走到 error_handler（如 MCP 配额耗尽 / 路线参数错误）：没有有效行程。
                        # 主动抛出，让 Celery 标记 FAILURE、状态端点返回 error，而不是用空 plan 伪装成功。
                        raise RuntimeError(error or "规划失败：未能生成有效行程")

                    await _set_trip_status(
                        session, trip_uuid, status="done", plan_json=plan, token_usage=token_usage
                    )
                    return plan
                except Exception as exc:
                    # 任何失败（运行异常 / 无有效行程）都落库为 error 状态，便于审计与 /history。
                    await session.rollback()
                    await _set_trip_status(
                        session, trip_uuid, status="error", error_msg=str(exc)
                    )
                    raise

    # Celery prefork worker 的进程内没有运行中的事件循环，这里新建一个并设为当前 loop，
    # 让底层库（redis.asyncio / asyncpg 等）能拿到正确的 loop。
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    ACTIVE_TASKS.inc()
    started = time.perf_counter()
    try:
        plan = loop.run_until_complete(_run())
        TRIPS_COMPLETED.labels(status="done").inc()
        _publish_to_channel(channel, {"type": "done", "plan": plan})
        logger.info("plan_trip_done", task_id=task_id)
        return plan
    except Exception as exc:  # noqa: BLE001 —— 含运行异常与「无有效行程」两种失败
        TRIPS_COMPLETED.labels(status="error").inc()
        logger.error("plan_trip_error", task_id=task_id, error=str(exc))
        _publish_to_channel(channel, {"type": "error", "message": str(exc)})
        raise
    finally:
        # 成功 / 失败都记规划总时延，并把活跃任务计数减回去。
        PLANNING_DURATION.observe(time.perf_counter() - started)
        ACTIVE_TASKS.dec()
        loop.close()
        asyncio.set_event_loop(None)
