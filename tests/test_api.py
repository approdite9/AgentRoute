"""
TASK D —— FastAPI 端点测试（httpx.AsyncClient + ASGITransport）。

外部依赖一律 mock / override：
  - DB：override get_db 依赖，返回一个只支持 add()/commit() 的假会话（不连真库）。
  - Celery：patch plan_trip_task.delay，返回带 .id 的假结果（不进真 broker）。
  - 健康检查：patch _check_redis/_check_mcp 为 True（不外连 MCP，结果可预期）。
  - 限流：走真实 Redis(db 4)；autouse 夹具在每个用例前清空 ratelimit:* 计数。
"""
import uuid
from unittest.mock import patch, MagicMock, AsyncMock

import httpx
import pytest
import redis.asyncio as aioredis

from config import settings
import api.routers.trips as trips_mod
import api.routers.health as health_mod
from api.main import app
from db.session import get_db

pytestmark = pytest.mark.anyio


class _FakeSession:
    """假 AsyncSession：create_trip 只用到 add()（同步）与 commit()（异步）。"""

    def add(self, obj):  # noqa: D401 - 简单占位
        pass

    async def commit(self):
        pass


async def _override_get_db():
    yield _FakeSession()


@pytest.fixture(autouse=True)
async def _reset_ratelimit():
    """每个用例前清空限流计数（db 4），避免上一个用例的请求把窗口计数带进来。"""
    client = aioredis.from_url(settings.redis_url, db=4, decode_responses=True)
    async for key in client.scan_iter("ratelimit:*"):
        await client.delete(key)
    yield
    await client.aclose()


@pytest.fixture
async def client():
    """绑定到 app 的异步 HTTP 客户端；DB 依赖被替换为假会话。"""
    app.dependency_overrides[get_db] = _override_get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.pop(get_db, None)


async def test_health_returns_ok(client):
    """GET /health → 200 且 status == "ok"（redis/mcp 探活均 mock 为可用）。"""
    with patch.object(health_mod, "_check_redis", return_value=True), patch.object(
        health_mod, "_check_mcp", return_value=True
    ):
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_submit_trip_returns_task_id(client):
    """POST /api/v1/trips → 200 且响应含 task_id（Celery 派发被 mock）。"""
    fake = MagicMock()
    fake.id = f"task-{uuid.uuid4()}"
    with patch.object(trips_mod.plan_trip_task, "delay", return_value=fake) as delay:
        resp = await client.post("/api/v1/trips", json={"city": "北京"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["task_id"] == fake.id
    assert body["status"] == "pending"
    delay.assert_called_once()


async def test_rate_limit(client):
    """连续提交超过阈值：第 (limit+1) 次返回 429。"""
    limit = settings.rate_limit_per_minute
    fake = MagicMock()
    fake.id = "task-rl"
    with patch.object(trips_mod.plan_trip_task, "delay", return_value=fake):
        # 前 limit 次应当全部放行（非 429）。
        for i in range(limit):
            resp = await client.post("/api/v1/trips", json={"city": "北京"})
            assert resp.status_code != 429, f"第 {i + 1} 次不应被限流"
        # 第 limit+1 次触发限流。
        resp = await client.post("/api/v1/trips", json={"city": "北京"})
    assert resp.status_code == 429
    assert resp.headers.get("Retry-After") is not None


async def test_metrics_endpoint(client):
    """GET /metrics → 200 且包含业务指标名 trips_submitted_total。"""
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "trips_submitted_total" in resp.text


async def test_clarify_returns_questions(client):
    """POST /api/v1/trips/clarify → 200，返回 LLM 生成的澄清问题（LLM 调用被 mock）。"""
    qs = [{"id": "q0", "question": "出行同伴？", "kind": "single",
           "options": ["独自", "家庭带娃", "朋友结伴"]}]
    with patch("agents.clarify.generate_questions", new=AsyncMock(return_value=qs)):
        resp = await client.post(
            "/api/v1/trips/clarify", json={"city": "三亚", "preferences": ["海滨"]}
        )
    assert resp.status_code == 200
    assert resp.json()["questions"] == qs


async def test_clarify_graceful_on_error(client):
    """澄清问题生成失败时端点降级为空列表（200，不抛 500），不阻塞后续规划。"""
    with patch(
        "agents.clarify.generate_questions",
        new=AsyncMock(side_effect=RuntimeError("LLM down")),
    ):
        resp = await client.post("/api/v1/trips/clarify", json={"city": "三亚"})
    assert resp.status_code == 200
    assert resp.json()["questions"] == []


async def test_trip_detail_requires_token(client):
    """历史详情端点未带 demo token → 401（鉴权在任何 DB 访问之前）。"""
    import uuid as _uuid
    resp = await client.get(f"/api/v1/trips/history/{_uuid.uuid4()}")
    assert resp.status_code == 401


async def test_trip_history_requires_no_token_returns_empty(client):
    """历史列表未带 token → 返回空列表（不报错）。"""
    resp = await client.get("/api/v1/trips/history")
    assert resp.status_code == 200
    assert resp.json() == []
