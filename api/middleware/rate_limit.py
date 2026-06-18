"""
基于 Redis 的滑动窗口限流中间件（按客户端 IP + 当前分钟计数）。

算法：
  key = ratelimit:{client_ip}:{minute}
  INCR key；首次设置时 EXPIRE 60s。
  若 count > settings.rate_limit_per_minute → 返回 429 + Retry-After。

说明：
  - 计数库用 db=4，与缓存/pub-sub 隔离。
  - /health、/metrics、/docs 等基础设施端点不计入限流，避免健康探测被误伤。
  - Redis 不可用时「放行」（fail-open）：限流是保护手段，不应因其故障而拒绝全部流量。
"""
import time

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from api.deps import get_ratelimit_redis
from config import settings

logger = structlog.get_logger(__name__)

# 这些路径不参与限流。
_EXEMPT_PREFIXES = ("/health", "/metrics", "/docs", "/redoc", "/openapi.json")


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith(_EXEMPT_PREFIXES):
            return await call_next(request)

        # X-Forwarded-For contains the real client IP when behind Railway's proxy.
        x_forwarded_for = request.headers.get("x-forwarded-for")
        if x_forwarded_for:
            client_ip = x_forwarded_for.split(",")[0].strip()
        elif request.client:
            client_ip = request.client.host
        else:
            client_ip = "unknown"
        now = int(time.time())
        window = now // 60
        key = f"ratelimit:{client_ip}:{window}"

        try:
            redis = get_ratelimit_redis()
            count = await redis.incr(key)
            if count == 1:
                # 仅在窗口内首次出现时设置过期，避免每次请求都刷新 TTL。
                await redis.expire(key, 60)
        except Exception as exc:  # noqa: BLE001 —— Redis 故障时 fail-open
            logger.warning("ratelimit_redis_failed", error=str(exc))
            return await call_next(request)

        if count > settings.rate_limit_per_minute:
            retry_after = 60 - (now % 60)
            logger.info("rate_limited", client_ip=client_ip, count=count)
            return JSONResponse(
                status_code=429,
                content={
                    "detail": "Rate limit exceeded. Try again later.",
                    "limit": settings.rate_limit_per_minute,
                    "retry_after": retry_after,
                },
                headers={"Retry-After": str(retry_after)},
            )

        return await call_next(request)
