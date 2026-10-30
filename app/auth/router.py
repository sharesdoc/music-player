"""
认证路由模块
POST /api/auth/register  - 注册
POST /api/auth/login     - 登录
POST /api/auth/logout    - 注销
GET  /api/auth/me        - 获取当前用户信息
POST /api/auth/refresh   - 刷新 Token
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr, Field

from app.auth import service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["认证"])
bearer = HTTPBearer(auto_error=False)


# ── 请求/响应 Schema ──────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: EmailStr
    username: str = Field(min_length=2, max_length=32, pattern=r'^[a-zA-Z0-9_\u4e00-\u9fff]+$')
    password: str = Field(min_length=6, max_length=128)


class LoginRequest(BaseModel):
    email_or_username: str = Field(min_length=2, max_length=128)
    password: str = Field(min_length=1, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str


# ── 当前用户依赖 ──────────────────────────────────────────────────────────

def get_current_user(credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer)) -> dict:
    """
    FastAPI 依赖：从 Authorization: Bearer <token> 解析当前用户
    未登录时抛出 401；管理员 Token 返回合成用户对象，不查数据库
    """
    if not credentials:
        raise HTTPException(status_code=401, detail="未登录，请先登录")

    token = credentials.credentials
    payload = service.decode_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Token 无效或已过期")

    if service.is_token_blacklisted(payload.get('jti', '')):
        raise HTTPException(status_code=401, detail="Token 已失效，请重新登录")

    # 管理员 Token：返回合成用户，不查数据库
    if payload.get('is_admin'):
        return {
            'id': 0,
            'username': payload.get('username', 'admin'),
            'email': f"{payload.get('username', 'admin')}@system",
            'is_admin': 1,
            'upload_count': 0,
            'download_credits': 0,
        }

    user = service.get_user_by_id(int(payload['sub']))
    if not user:
        raise HTTPException(status_code=401, detail="用户不存在")

    return user


def require_admin(current_user: dict = Depends(get_current_user)) -> dict:
    """FastAPI 依赖：要求管理员身份，否则 403"""
    if not current_user.get('is_admin'):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return current_user


def get_current_user_optional(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer)
) -> Optional[dict]:
    """可选认证：未登录时返回 None，不报错"""
    if not credentials:
        return None
    try:
        return get_current_user(credentials)
    except HTTPException:
        return None


# ── 路由处理器 ────────────────────────────────────────────────────────────

@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(req: RegisterRequest):
    """
    用户注册
    - 邮箱唯一，用户名唯一
    - 密码 6-128 位
    """
    try:
        user = service.register_user(req.email, req.username, req.password)
        # 注册成功直接颁发 Token
        access_token = service.create_access_token(user['id'], user['username'])
        refresh_token = service.create_refresh_token(user['id'])
        return {
            'message': '注册成功',
            'user': {k: v for k, v in user.items() if k not in ('password_hash', 'salt')},
            'access_token': access_token,
            'refresh_token': refresh_token,
            'token_type': 'Bearer',
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/login")
async def login(req: LoginRequest):
    """
    用户登录
    - 支持邮箱或用户名
    - 失败超过限制会锁定账号
    """
    try:
        result = service.login_user(req.email_or_username, req.password)
        return result
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))


@router.post("/logout")
async def logout(current_user: dict = Depends(get_current_user),
                 credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer)):
    """注销：将当前 Token 加入黑名单"""
    if credentials:
        token = credentials.credentials
        payload = service.decode_token(token)
        if payload:
            from datetime import timedelta
            exp = datetime.fromtimestamp(payload['exp'], tz=timezone.utc)
            service.blacklist_token(payload['jti'], exp)
    logger.info(f"用户注销: id={current_user['id']}, username={current_user['username']}")
    return {'message': '已成功注销'}


@router.get("/me")
async def me(current_user: dict = Depends(get_current_user)):
    """获取当前登录用户信息"""
    # 过滤敏感字段
    return {k: v for k, v in current_user.items() if k not in ('password_hash', 'salt')}


@router.post("/refresh")
async def refresh(req: RefreshRequest):
    """使用 Refresh Token 换取新的 Access Token"""
    try:
        result = service.refresh_access_token(req.refresh_token)
        return result
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
