"""
ORM 模型 —— 行程计划（trip_plans）与审计日志（audit_logs）。

  trip_plans：每次规划请求一行，记录输入、最终行程 JSON、状态机与 token 用量。
  audit_logs：规划过程中的事件流（每个图节点完成一条），与 trip_plans 一对多、级联删除。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.session import Base


def _utcnow() -> datetime:
    """带时区的 UTC now —— 与 DateTime(timezone=True) 列匹配，避免 utcnow 的弃用与 naive 比较。"""
    return datetime.now(timezone.utc)


class TripPlan(Base):
    __tablename__ = "trip_plans"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    thread_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(64), index=True)
    city: Mapped[str] = mapped_column(String(128), nullable=False)
    start_date: Mapped[str] = mapped_column(String(16))
    end_date: Mapped[str] = mapped_column(String(16))
    preferences: Mapped[dict | None] = mapped_column(JSON)
    plan_json: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    error_msg: Mapped[str | None] = mapped_column(Text)
    token_usage: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    audit_logs: Mapped[list["AuditLog"]] = relationship(
        back_populates="trip", cascade="all, delete-orphan"
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trip_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("trip_plans.id"), index=True
    )
    event: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    trip: Mapped["TripPlan"] = relationship(back_populates="audit_logs")


class DemoUser(Base):
    """演示用户准入表 —— 每个注册用户一行，控制试用配额与状态。"""

    __tablename__ = "demo_users"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    email: Mapped[str] = mapped_column(String(256), index=True, nullable=False)
    purpose: Mapped[str | None] = mapped_column(Text)
    quota: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    used_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    admin_notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
