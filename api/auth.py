"""集中式鉴权依赖 —— 保护写接口，杜绝匿名调用刷爆配额/账单。

设计（面试级、可平滑升级到企业级）：
  - 凭证：HTTP `Authorization: Bearer <token>`，兼容历史的 `x-demo-token` 头。
    目前 token 即演示注册令牌；结构上是「Bearer 令牌鉴权」，将来把校验逻辑换成
    JWT 验签 / OAuth 引入即可，**接口签名不变**（Depends(get_current_user)）。
  - 认证（你是谁）：令牌必须对应一个存在的 DemoUser，否则 401。
  - 授权（你能干什么）：被封禁用户 403；管理员接口另由 demo_access._verify_admin（密码）守护。
  - 统一以依赖注入方式挂到路由上，而非每个端点各写一遍——集中、可测、不易漏。
"""
from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import DemoUser
from db.session import get_db


def _extract_token(authorization: str, x_demo_token: str) -> str:
    """优先取 `Authorization: Bearer <token>`，回退到 `x-demo-token` 头。"""
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return (x_demo_token or "").strip()


async def get_current_user(
    authorization: str = Header(default=""),
    x_demo_token: str = Header(default=""),
    db: AsyncSession = Depends(get_db),
) -> DemoUser:
    """认证 + 基本授权：校验令牌 → 返回 DemoUser；无效 401、封禁 403。"""
    token = _extract_token(authorization, x_demo_token)
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = (
        await db.execute(select(DemoUser).where(DemoUser.token == token))
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if user.is_blocked:
        raise HTTPException(status_code=403, detail="Account is blocked")
    return user
