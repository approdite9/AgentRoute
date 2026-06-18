"""
行程路由 —— 提交规划、查询状态、SSE 流式订阅。

  POST /api/v1/trips               提交规划请求 → {task_id, status}
  GET  /api/v1/trips/{task_id}     查询任务状态 + 结果
  GET  /api/v1/trips/{task_id}/stream  SSE：实时推送 token / 工具事件 / 最终 plan
"""
import asyncio
import json
import uuid

import structlog
from celery.result import AsyncResult
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_pubsub_redis
from db.models import DemoUser, TripPlan
from db.session import get_db
from monitoring.metrics import TRIPS_SUBMITTED
from tasks.celery_app import celery_app
from tasks.trip_tasks import plan_trip_task

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["trips"])


class TripRequest(BaseModel):
    """行程规划请求体 —— 字段与 agents.state.TripState 的输入字段对齐。"""

    city: str
    start_date: str = ""
    end_date: str = ""
    preferences: list[str] = Field(default_factory=list)
    hotel_type: str = ""
    transport: list[str] = Field(default_factory=list)
    extra: str = ""
    # 旅行人群画像（影响景点/酒店/餐厅与节奏推荐）。
    party_type: str = ""
    party_size: int = 0
    budget_level: str = ""
    # 出发地与往返时间（手动输入，无票务 API）：驱动往返交通段 + 首尾日半天截断。
    origin_city: str = ""
    arrival_time: str = ""
    departure_time: str = ""
    user_id: str | None = None
    # 多轮修改：带上一版成稿（prev_plan）+ 修改要求（feedback）时，图入口直达
    # synthesize，跳过 weather/poi/hotel/route 的重复采集，只在原计划上做最小修改。
    prev_plan: dict | None = None
    feedback: str = ""


# Celery 任务状态 → 对外统一状态词。
_STATUS_MAP = {
    "PENDING": "pending",
    "RECEIVED": "pending",
    "STARTED": "running",
    "RETRY": "running",
    "SUCCESS": "done",
    "FAILURE": "error",
    "REVOKED": "error",
}


def _build_initial_state(req: TripRequest) -> dict:
    """把请求体转成 LangGraph 图的初始 state_dict。"""
    state: dict = {
        "city": req.city,
        "start_date": req.start_date,
        "end_date": req.end_date,
        "preferences": req.preferences,
        "hotel_type": req.hotel_type,
        "transport": req.transport,
        "extra": req.extra,
        "party_type": req.party_type,
        "party_size": req.party_size,
        "budget_level": req.budget_level,
        "origin_city": req.origin_city,
        "arrival_time": req.arrival_time,
        "departure_time": req.departure_time,
        "messages": [],
        "retry_count": 0,
    }
    # 多轮修改：同时带来上一版计划与修改要求 → 注入 final_plan + user_feedback，
    # 触发 entry_router 直达 synthesize 的「最小修改」路径（省去重复采集的 token）。
    if req.prev_plan and req.feedback.strip():
        state["final_plan"] = req.prev_plan
        state["user_feedback"] = req.feedback.strip()
    return state


@router.post("/trips")
async def create_trip(req: TripRequest, db: AsyncSession = Depends(get_db)) -> dict:
    """先把 pending 行入库，再派发规划任务到 Celery，立即返回（不阻塞 HTTP）。"""
    state_dict = _build_initial_state(req)

    # 预生成主键：让 thread_id、graph 的 checkpoint thread、Celery 任务参数三者对齐，
    # 且必须先 commit pending 行，worker（独立进程/连接）才能 UPDATE 到它（避免竞态）。
    trip_id = uuid.uuid4()
    trip = TripPlan(
        id=trip_id,
        thread_id=str(trip_id),
        user_id=req.user_id,
        city=req.city,
        start_date=req.start_date,
        end_date=req.end_date,
        preferences={
            "preferences": req.preferences,
            "hotel_type": req.hotel_type,
            "transport": req.transport,
            "extra": req.extra,
            "party_type": req.party_type,
            "party_size": req.party_size,
            "budget_level": req.budget_level,
            "origin_city": req.origin_city,
            "arrival_time": req.arrival_time,
            "departure_time": req.departure_time,
        },
        status="pending",
    )
    db.add(trip)
    await db.commit()

    async_result = plan_trip_task.delay(state_dict, str(trip_id))
    TRIPS_SUBMITTED.inc()
    logger.info("trip_dispatched", task_id=async_result.id, trip_id=str(trip_id), city=req.city)
    return {"task_id": async_result.id, "trip_id": str(trip_id), "status": "pending"}


@router.post("/trips/clarify")
async def clarify_trip(req: TripRequest) -> dict:
    """规划前主动追问：据初步需求返回 3-4 个澄清问题（单次 LLM 调用，无 MCP）。

    失败（LLM 异常/超时）时降级为空列表，前端据此直接进入规划，不阻塞主流程。
    方法为 POST 且路径固定，与 GET /trips/{task_id} 不冲突，无需考虑路由顺序。
    """
    from agents.clarify import generate_questions

    try:
        questions = await generate_questions(req.model_dump())
    except Exception as exc:  # noqa: BLE001 —— 追问是增强项，失败不应中断规划
        logger.warning("clarify_failed", error=str(exc), city=req.city)
        questions = []
    return {"questions": questions}


@router.get("/trips/history")
async def trip_history(
    x_demo_token: str = Header(default=""),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """
    返回当前 token 用户的最近 20 条规划历史。

    路由顺序很关键：必须声明在 /trips/{task_id} 之前，否则 "history" 会被当作 task_id。
    """
    if not x_demo_token:
        return []
    user = (
        await db.execute(select(DemoUser).where(DemoUser.token == x_demo_token))
    ).scalar_one_or_none()
    if not user or user.is_blocked:
        return []
    stmt = (
        select(TripPlan)
        .where(TripPlan.user_id == x_demo_token)
        .order_by(TripPlan.created_at.desc())
        .limit(20)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(r.id),
            "city": r.city,
            "start_date": r.start_date,
            "end_date": r.end_date,
            "status": r.status,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@router.get("/trips/history/{trip_id}")
async def trip_detail(
    trip_id: str,
    x_demo_token: str = Header(default=""),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """返回当前 token 用户某条历史行程的**完整计划**（plan_json）。

    仅限本人查看（trip.user_id 必须等于该 token），与配额无关——配额用完仍可回看
    自己已生成的行程。路径含字面量 "history" 段，不与 /trips/{task_id} 冲突。
    """
    if not x_demo_token:
        raise HTTPException(status_code=401, detail="Demo token required")
    user = (
        await db.execute(select(DemoUser).where(DemoUser.token == x_demo_token))
    ).scalar_one_or_none()
    if not user or user.is_blocked:
        raise HTTPException(status_code=403, detail="Invalid or blocked token")
    try:
        tid = uuid.UUID(trip_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid trip id")
    trip = (
        await db.execute(select(TripPlan).where(TripPlan.id == tid))
    ).scalar_one_or_none()
    # 越权防护：只能看自己的（user_id == 当前 token）。
    if not trip or trip.user_id != x_demo_token:
        raise HTTPException(status_code=404, detail="Trip not found")
    return {
        "id": str(trip.id),
        "city": trip.city,
        "start_date": trip.start_date,
        "end_date": trip.end_date,
        "status": trip.status,
        "plan": trip.plan_json,
    }


@router.get("/trips/{task_id}")
async def get_trip(task_id: str) -> dict:
    """查询 Celery result backend 中的任务状态与结果。"""
    result = AsyncResult(task_id, app=celery_app)
    status = _STATUS_MAP.get(result.state, "pending")

    payload: dict = {"task_id": task_id, "status": status, "result": None}
    if status == "done":
        payload["result"] = result.result
    elif status == "error":
        # result.result 是异常对象，转成可序列化的字符串。
        payload["error"] = str(result.result)
    return payload


async def _event_stream(task_id: str):
    """订阅 stream:{task_id}，把 pub/sub 消息逐条转成 SSE 帧。"""
    channel = f"stream:{task_id}"
    redis = get_pubsub_redis()
    pubsub = redis.pubsub()
    await pubsub.subscribe(channel)
    try:
        # 起手先发一个 ping 注释帧，促使代理/浏览器立即建立连接。
        yield ": connected\n\n"
        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=1.0
            )
            if message is None:
                # 没有新消息：发心跳注释帧，保持连接 + 探测客户端是否断开。
                yield ": keepalive\n\n"
                continue

            data = message["data"]
            yield f"data: {data}\n\n"

            # 收到终止信号后结束流。
            try:
                parsed = json.loads(data)
            except (TypeError, json.JSONDecodeError):
                parsed = {}
            if parsed.get("type") in ("done", "error"):
                break
    except asyncio.CancelledError:
        # 客户端断开 —— 正常收尾。
        raise
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()


@router.get("/trips/{task_id}/stream")
async def stream_trip(
    task_id: str,
    x_demo_token: str = Header(default=""),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """SSE 端点：订阅 Redis pub/sub，把规划过程实时推给浏览器。"""
    if not x_demo_token:
        raise HTTPException(status_code=401, detail="Demo token required")
    user = (
        await db.execute(select(DemoUser).where(DemoUser.token == x_demo_token))
    ).scalar_one_or_none()
    if not user or user.is_blocked:
        raise HTTPException(status_code=403, detail="Invalid or blocked token")
    return StreamingResponse(
        _event_stream(task_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 关闭 Nginx 缓冲，token 才能即时透传
        },
    )
