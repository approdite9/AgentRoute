"""
FastAPI 应用入口 —— 路由注册 + Prometheus 指标 + 限流中间件。

启动：
    uvicorn api.main:app --port 8000
"""
from contextlib import asynccontextmanager

# 必须在创建 app / 任何业务逻辑之前配置好结构化日志与追踪环境。
from logging_config import configure_logging

configure_logging()

from fastapi import FastAPI, Response

from api.deps import close_redis
from api.middleware.rate_limit import RateLimitMiddleware
from api.routers import health, trips
from api.routers.demo_access import router as demo_router, admin_router
from config import settings
from db.session import engine
from monitoring.metrics import render_latest


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动阶段：暂无需预热的资源（redis 客户端 / DB 引擎均按需懒加载）。
    yield
    # 关闭阶段：优雅释放 redis.asyncio 连接与数据库连接池。
    await close_redis()
    await engine.dispose()


app = FastAPI(title="Travel Agent API", version="1.0.0", lifespan=lifespan)

# Sentry 错误追踪（TASK D）：仅在配置了 DSN 时启用。把 import 放进守卫内，
# 这样未配置 DSN 时不强依赖 sentry_sdk，也不会有任何上报开销。
if settings.sentry_dsn:
    import sentry_sdk
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.celery import CeleryIntegration

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        integrations=[FastApiIntegration(), CeleryIntegration()],
        traces_sample_rate=0.1,
    )

# 限流中间件（最外层，先于业务逻辑拦截）。
app.add_middleware(RateLimitMiddleware)

@app.get("/metrics")
def metrics() -> Response:
    """暴露 Prometheus 指标；多进程模式下聚合所有进程（含 Celery worker）。"""
    data, content_type = render_latest()
    return Response(content=data, media_type=content_type)


app.include_router(trips.router, prefix="/api/v1")
app.include_router(demo_router, prefix="/api/v1")
app.include_router(admin_router, prefix="/admin")
app.include_router(health.router)
