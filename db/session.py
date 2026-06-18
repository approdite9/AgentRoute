"""
异步数据库会话 —— SQLAlchemy 2.0 + asyncpg。

两类引擎，按运行环境隔离，避免「Future attached to a different loop」：
  - engine / AsyncSessionLocal：FastAPI 进程长驻、单事件循环，用带连接池的共享引擎。
  - worker_session()：Celery prefork worker 每个任务现建一个事件循环；asyncpg 的连接
    绑定到创建它的 loop，跨 loop 复用连接池会报错。故每个任务用 NullPool 现建现弃的
    引擎，任务结束即 dispose。
"""
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from config import settings


def _require_db_url() -> str:
    url = settings.database_url
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. "
            "Add it in Railway Dashboard > AgentRoute > Variables."
        )
    return url


# FastAPI 进程内共享的带池引擎（单一长驻事件循环）。
# 懒加载：导入时不建连接，首次调用 get_db() 时才创建，避免缺 URL 时启动即崩。
engine = create_async_engine(_require_db_url(), pool_size=10, max_overflow=20)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：每个请求一个会话，结束自动关闭。"""
    async with AsyncSessionLocal() as session:
        yield session


@asynccontextmanager
async def worker_session() -> AsyncIterator[AsyncSession]:
    """
    Celery 任务专用会话：现建 NullPool 引擎 → 会话 → 任务结束 dispose。

    每个 Celery 任务跑在自己新建的事件循环里，绝不能复用 FastAPI 那个共享池引擎
    （连接绑定在别的 loop 上）。NullPool 不缓存连接，配合 dispose() 彻底释放。
    """
    task_engine = create_async_engine(settings.database_url, poolclass=NullPool)
    maker = async_sessionmaker(task_engine, expire_on_commit=False)
    try:
        async with maker() as session:
            yield session
    finally:
        await task_engine.dispose()
