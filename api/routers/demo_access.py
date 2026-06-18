"""
演示准入控制路由

  POST /api/v1/demo/register      注册新用户，返回访问 token
  POST /api/v1/demo/check         用 token 查询剩余配额
  POST /api/v1/demo/use           消耗一次配额（规划成功后调用）
  GET  /admin/demo/users          管理员：查看所有用户
  POST /admin/demo/approve/{uid}  管理员：增加配额
  POST /admin/demo/block/{uid}    管理员：封禁用户
"""
import hmac
import os
import secrets
import uuid
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import DemoUser
from db.session import get_db

logger = structlog.get_logger(__name__)

# 管理员密码从环境变量读取；未配置时管理接口全部拒绝。
_ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

router = APIRouter(tags=["demo"])
admin_router = APIRouter(tags=["admin"])


# ---------- Pydantic schemas ----------

class RegisterRequest(BaseModel):
    name: str
    email: str  # 格式校验在 Streamlit 前端做，后端只存储
    purpose: str = ""


class CheckRequest(BaseModel):
    token: str


class UseRequest(BaseModel):
    token: str


class ApproveRequest(BaseModel):
    extra_quota: int = 1
    notes: str = ""


# ---------- helpers ----------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _verify_admin(x_admin_password: str = Header(default="")):
    if not _ADMIN_PASSWORD:
        raise HTTPException(status_code=503, detail="管理密码未配置，管理接口不可用")
    if not hmac.compare_digest(x_admin_password, _ADMIN_PASSWORD):
        raise HTTPException(status_code=403, detail="管理密码错误")


# ---------- 用户端接口 ----------

@router.post("/demo/register")
async def register(req: RegisterRequest, db: AsyncSession = Depends(get_db)) -> dict:
    """注册新演示用户。同一邮箱只能注册一次（大小写不敏感）。"""
    email_lower = req.email.strip().lower()

    existing = (
        await db.execute(
            select(DemoUser).where(func.lower(DemoUser.email) == email_lower)
        )
    ).scalar_one_or_none()

    if existing:
        if existing.is_blocked:
            raise HTTPException(status_code=403, detail="该账号已被封禁，请联系管理员")
        # 邮箱已注册：返回原 token，方便用户找回（不暴露剩余次数给前端做提示）
        return {
            "token": existing.token,
            "already_registered": True,
            "can_use": existing.used_count < existing.quota and not existing.is_blocked,
        }

    token = secrets.token_urlsafe(32)
    user = DemoUser(
        id=uuid.uuid4(),
        token=token,
        name=req.name.strip(),
        email=email_lower,
        purpose=req.purpose.strip() or None,
        quota=1,
        used_count=0,
        is_blocked=False,
        created_at=_utcnow(),
    )
    db.add(user)
    await db.commit()
    logger.info("demo_user_registered", user_id=str(user.id))
    return {"token": token, "already_registered": False, "can_use": True}


@router.post("/demo/check")
async def check(req: CheckRequest, db: AsyncSession = Depends(get_db)) -> dict:
    """检查 token 状态与剩余配额。"""
    user = (
        await db.execute(select(DemoUser).where(DemoUser.token == req.token))
    ).scalar_one_or_none()

    if not user:
        return {"valid": False, "can_use": False, "reason": "token_invalid"}
    if user.is_blocked:
        return {"valid": True, "can_use": False, "reason": "blocked", "name": user.name}

    remaining = user.quota - user.used_count
    return {
        "valid": True,
        "can_use": remaining > 0,
        "reason": "ok" if remaining > 0 else "quota_exhausted",
        "name": user.name,
        "used": user.used_count,
        "quota": user.quota,
        "remaining": max(remaining, 0),
    }


@router.post("/demo/use")
async def use_quota(req: UseRequest, db: AsyncSession = Depends(get_db)) -> dict:
    """消耗一次配额。规划任务成功提交后由前端调用。"""
    user = (
        await db.execute(select(DemoUser).where(DemoUser.token == req.token))
    ).scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=404, detail="token 无效")
    if user.is_blocked:
        raise HTTPException(status_code=403, detail="账号已封禁")
    if user.used_count >= user.quota:
        raise HTTPException(status_code=429, detail="试用次数已用完")

    user.used_count += 1
    user.last_used_at = _utcnow()
    await db.commit()
    logger.info("demo_quota_used", user_id=str(user.id), used=user.used_count, quota=user.quota)
    return {"ok": True, "remaining": user.quota - user.used_count}


# ---------- 管理员接口 ----------

@admin_router.get("/demo/users")
async def list_users(
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_verify_admin),
) -> list[dict]:
    """返回所有演示用户列表，按注册时间倒序。"""
    rows = (
        await db.execute(select(DemoUser).order_by(DemoUser.created_at.desc()))
    ).scalars().all()
    return [
        {
            "id": str(r.id),
            "name": r.name,
            "email": r.email,
            "purpose": r.purpose,
            "quota": r.quota,
            "used_count": r.used_count,
            "remaining": max(r.quota - r.used_count, 0),
            "is_blocked": r.is_blocked,
            "admin_notes": r.admin_notes,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None,
        }
        for r in rows
    ]


@admin_router.post("/demo/approve/{user_id}")
async def approve_user(
    user_id: str,
    req: ApproveRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_verify_admin),
) -> dict:
    """给指定用户追加配额，并写管理备注。"""
    user = (
        await db.execute(
            select(DemoUser).where(DemoUser.id == uuid.UUID(user_id))
        )
    ).scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    user.quota += req.extra_quota
    user.is_blocked = False
    if req.notes:
        user.admin_notes = req.notes
    await db.commit()
    logger.info("demo_user_approved", user_id=user_id, new_quota=user.quota)
    return {"ok": True, "new_quota": user.quota, "remaining": user.quota - user.used_count}


@admin_router.post("/demo/block/{user_id}")
async def block_user(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_verify_admin),
) -> dict:
    """封禁指定用户（不删除记录）。"""
    user = (
        await db.execute(
            select(DemoUser).where(DemoUser.id == uuid.UUID(user_id))
        )
    ).scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    user.is_blocked = True
    await db.commit()
    logger.info("demo_user_blocked", user_id=user_id)
    return {"ok": True}
