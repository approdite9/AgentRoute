"""
健康检查路由 —— /health 探活 Redis 与 MCP，供 k8s/负载均衡器使用。

  GET /health → {"status": "ok"|"degraded", "redis": bool, "mcp": bool, "timestamp": iso}
"""
import asyncio
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter

from api.deps import get_health_redis
from mcp_client import McpClientManager

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["health"])


async def _check_redis() -> bool:
    try:
        return bool(await get_health_redis().ping())
    except Exception as exc:  # noqa: BLE001
        logger.warning("health_redis_failed", error=str(exc))
        return False


async def _check_mcp() -> bool:
    try:
        tools = await asyncio.wait_for(
            McpClientManager().get_tools_for("weather"), timeout=5.0
        )
        return len(tools) > 0
    except Exception as exc:  # noqa: BLE001 —— 含 asyncio.TimeoutError
        logger.warning("health_mcp_failed", error=str(exc))
        return False


@router.get("/health")
async def health() -> dict:
    """并发探活 Redis 与 MCP；任一不可用则整体 degraded。"""
    redis_ok, mcp_ok = await asyncio.gather(_check_redis(), _check_mcp())
    return {
        "status": "ok" if (redis_ok and mcp_ok) else "degraded",
        "redis": redis_ok,
        "mcp": mcp_ok,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
