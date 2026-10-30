"""
认证服务模块
- 用户注册、登录、Token管理
- 密码：sha1(salt + password)，40位哈希
- JWT：Access Token(1h) + Refresh Token(7d)
"""
import hashlib
import secrets
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt

from app.config import config
from app.database import db, row_to_dict

logger = logging.getLogger(__name__)


# ── 密码工具 ──────────────────────────────────────────────────────────────

def _generate_salt(length: int = 16) -> str:
    """生成随机 salt（16字节十六进制）"""
    return secrets.token_hex(length)


def hash_password(password: str, salt: str) -> str:
    """
    密码哈希：sha1(salt + password)
    返回 40 位十六进制哈希值，符合 CLAUDE.md 规范
    """
    raw = (salt + password).encode('utf-8')
    return hashlib.sha1(raw).hexdigest()


def verify_password(password: str, salt: str, stored_hash: str) -> bool:
    """验证密码是否匹配"""
    return hash_password(password, salt) == stored_hash


# ── Token 工具 ────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def create_access_token(user_id: int, username: str) -> str:
    """创建 Access Token（有效期 JWT_EXPIRE_MINUTES 分钟）"""
    jti = secrets.token_hex(16)
    payload = {
        'sub': str(user_id),
        'username': username,
        'jti': jti,
        'type': 'access',
        'iat': _utcnow(),
        'exp': _utcnow() + timedelta(minutes=config.JWT_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)


def create_admin_token() -> str:
    """为超级管理员签发 JWT（不依赖数据库，通过 env 认证）"""
    jti = secrets.token_hex(16)
    payload = {
        'sub': '0',
        'username': config.ADMIN_USERNAME,
        'is_admin': True,
        'jti': jti,
        'type': 'access',
        'iat': _utcnow(),
        'exp': _utcnow() + timedelta(minutes=config.JWT_EXPIRE_MINUTES * 8),
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)


def create_refresh_token(user_id: int) -> str:
    """创建 Refresh Token（有效期 REFRESH_EXPIRE_DAYS 天）"""
    jti = secrets.token_hex(16)
    payload = {
        'sub': str(user_id),
        'jti': jti,
        'type': 'refresh',
        'iat': _utcnow(),
        'exp': _utcnow() + timedelta(days=config.REFRESH_EXPIRE_DAYS),
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)


def decode_token(token: str) -> Optional[dict]:
    """解码 Token，返回 payload 或 None（无效时）"""
    try:
        return jwt.decode(token, config.JWT_SECRET, algorithms=[config.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        logger.debug("Token 已过期")
        return None
    except jwt.InvalidTokenError as e:
        logger.debug(f"Token 无效: {e}")
        return None


def is_token_blacklisted(jti: str) -> bool:
    """检查 Token 是否已被加入黑名单（注销）"""
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM token_blacklist WHERE jti = ?", (jti,)
        ).fetchone()
        return row is not None


def blacklist_token(jti: str, expired_at: datetime):
    """将 Token 加入黑名单"""
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO token_blacklist (jti, expired_at) VALUES (?, ?)",
            (jti, expired_at.isoformat())
        )
    # 顺便清理过期的黑名单记录
    _cleanup_blacklist()


def _cleanup_blacklist():
    """清理已过期的黑名单记录（防止无限增长）"""
    with db() as conn:
        conn.execute(
            "DELETE FROM token_blacklist WHERE expired_at < datetime('now')"
        )


# ── 用户操作 ──────────────────────────────────────────────────────────────

def get_user_by_id(user_id: int) -> Optional[dict]:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return row_to_dict(row)


def get_user_by_email(email: str) -> Optional[dict]:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE email = ?", (email.lower().strip(),)
        ).fetchone()
        return row_to_dict(row)


def get_user_by_username(username: str) -> Optional[dict]:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username.strip(),)
        ).fetchone()
        return row_to_dict(row)


def register_user(email: str, username: str, password: str) -> dict:
    """
    注册新用户
    返回创建的用户信息
    抛出 ValueError 如果邮箱/用户名已存在
    """
    email = email.lower().strip()
    username = username.strip()

    # 检查是否已存在
    if get_user_by_email(email):
        raise ValueError("该邮箱已被注册")
    if get_user_by_username(username):
        raise ValueError("该用户名已被占用")

    salt = _generate_salt()
    pw_hash = hash_password(password, salt)

    with db() as conn:
        cursor = conn.execute(
            """INSERT INTO users (email, username, password_hash, salt)
               VALUES (?, ?, ?, ?)""",
            (email, username, pw_hash, salt)
        )
        user_id = cursor.lastrowid
        logger.info(f"新用户注册: id={user_id}, email={email}, username={username}")

    return get_user_by_id(user_id)


def login_user(email_or_username: str, password: str) -> dict:
    """
    用户登录
    返回 {'user': ..., 'access_token': ..., 'refresh_token': ...}
    抛出 ValueError 如果认证失败
    """
    # 超级管理员优先（通过 env 配置认证，不查数据库）
    # 支持明文密码或 SHA1 哈希（40位hex）两种格式
    def _admin_pwd_match(input_pwd, stored_pwd):
        if not stored_pwd:
            return False
        if input_pwd == stored_pwd:
            return True
        # 如果 env 存的是 40 位 hex（SHA1 哈希），用 sha1(input) 比对
        if len(stored_pwd) == 40 and all(c in '0123456789abcdef' for c in stored_pwd.lower()):
            return hashlib.sha1(input_pwd.encode('utf-8')).hexdigest() == stored_pwd.lower()
        return False

    if (config.ADMIN_USERNAME and config.ADMIN_PASSWORD
            and email_or_username.strip().lower() == config.ADMIN_USERNAME.lower()
            and _admin_pwd_match(password, config.ADMIN_PASSWORD)):
        token = create_admin_token()
        logger.info(f"管理员登录: {config.ADMIN_USERNAME}")
        return {
            'user': {
                'id': 0,
                'username': config.ADMIN_USERNAME,
                'email': f'{config.ADMIN_USERNAME}@system',
                'is_admin': 1,
                'upload_count': 0,
                'download_credits': 0,
            },
            'access_token': token,
            'refresh_token': token,
            'token_type': 'Bearer',
        }

    # 支持邮箱或用户名登录
    val = email_or_username.lower().strip()
    user = get_user_by_email(val) or get_user_by_username(email_or_username.strip())

    if not user:
        raise ValueError("用户不存在")

    # 检查账号是否被锁定
    if user['locked_until']:
        lock_time = datetime.fromisoformat(user['locked_until'])
        if lock_time.tzinfo is None:
            lock_time = lock_time.replace(tzinfo=timezone.utc)
        if _utcnow() < lock_time:
            remaining = int((lock_time - _utcnow()).total_seconds() / 60) + 1
            raise ValueError(f"账号已锁定，请 {remaining} 分钟后重试")

    # 验证密码
    if not verify_password(password, user['salt'], user['password_hash']):
        _increment_login_attempts(user['id'])
        attempts = user['login_attempts'] + 1
        if attempts >= config.LOGIN_MAX_ATTEMPTS:
            raise ValueError(f"密码错误，账号已被锁定 {config.LOGIN_LOCKOUT_MINUTES} 分钟")
        remaining = config.LOGIN_MAX_ATTEMPTS - attempts
        raise ValueError(f"密码错误，还剩 {remaining} 次尝试机会")

    # 登录成功，重置计数器
    _reset_login_attempts(user['id'])

    access_token = create_access_token(user['id'], user['username'])
    refresh_token = create_refresh_token(user['id'])

    logger.info(f"用户登录成功: id={user['id']}, username={user['username']}")
    return {
        'user': {k: v for k, v in user.items() if k not in ('password_hash', 'salt')},
        'access_token': access_token,
        'refresh_token': refresh_token,
        'token_type': 'Bearer',
    }


def _increment_login_attempts(user_id: int):
    """递增登录尝试次数，达到上限时锁定账号"""
    with db() as conn:
        conn.execute(
            "UPDATE users SET login_attempts = login_attempts + 1 WHERE id = ?",
            (user_id,)
        )
        row = conn.execute(
            "SELECT login_attempts FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row and row['login_attempts'] >= config.LOGIN_MAX_ATTEMPTS:
            lock_until = (_utcnow() + timedelta(minutes=config.LOGIN_LOCKOUT_MINUTES)).isoformat()
            conn.execute(
                "UPDATE users SET locked_until = ? WHERE id = ?",
                (lock_until, user_id)
            )
            logger.warning(f"账号 id={user_id} 因多次登录失败被锁定")


def _reset_login_attempts(user_id: int):
    """登录成功后重置尝试次数和锁定状态"""
    with db() as conn:
        conn.execute(
            """UPDATE users
               SET login_attempts = 0, locked_until = NULL,
                   last_login = datetime('now')
               WHERE id = ?""",
            (user_id,)
        )


def refresh_access_token(refresh_token: str) -> dict:
    """用 Refresh Token 换取新的 Access Token"""
    payload = decode_token(refresh_token)
    if not payload or payload.get('type') != 'refresh':
        raise ValueError("无效的 Refresh Token")
    if is_token_blacklisted(payload['jti']):
        raise ValueError("Token 已失效，请重新登录")

    user = get_user_by_id(int(payload['sub']))
    if not user:
        raise ValueError("用户不存在")

    new_access = create_access_token(user['id'], user['username'])
    return {'access_token': new_access, 'token_type': 'Bearer'}
