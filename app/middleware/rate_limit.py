"""
频率限制中间件（滑动窗口算法）
- 每IP每分钟最多 RATE_LIMIT_PER_MIN 次请求
- 超出则返回 429 Too Many Requests
- 使用 SQLite 存储，适合单机；多机环境建议换 Redis
"""
import logging
import time
from datetime import datetime, timedelta, timezone

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import config
from app.database import db

logger = logging.getLogger(__name__)

# 不限速的路径前缀（静态资源）
_SKIP_PATHS = {'/static', '/favicon.ico', '/health'}


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    滑动窗口频率限制中间件
    每 IP + action(path前缀) 组合限速
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        # 跳过静态资源
        path = request.url.path
        if any(path.startswith(p) for p in _SKIP_PATHS):
            return await call_next(request)

        ip = self._get_client_ip(request)
        action = self._get_action(path)

        if self._is_rate_limited(ip, action):
            logger.warning(f"频率超限: ip={ip}, path={path}")
            return Response(
                content='{"detail":"请求过于频繁，请稍后重试"}',
                status_code=429,
                media_type="application/json",
                headers={"Retry-After": str(config.RATE_LIMIT_WINDOW)}
            )

        # 记录本次请求
        self._log_request(ip, action)
        return await call_next(request)

    def _get_client_ip(self, request: Request) -> str:
        """获取真实客户端IP（支持 X-Forwarded-For）"""
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def _get_action(self, path: str) -> str:
        """将路径归为 action 类别，避免过细分拆"""
        if path.startswith("/api/auth"):
            return "auth"
        if path.startswith("/api/songs"):
            return "songs"
        return "api"

    def _is_rate_limited(self, ip: str, action: str) -> bool:
        """检查指定 IP+action 在时间窗口内的请求数是否超限"""
        window_start = (
            datetime.now(timezone.utc) - timedelta(seconds=config.RATE_LIMIT_WINDOW)
        ).isoformat()
        try:
            with db() as conn:
                row = conn.execute(
                    """SELECT COUNT(*) as cnt FROM rate_limit_log
                       WHERE ip = ? AND action = ? AND timestamp > ?""",
                    (ip, action, window_start)
                ).fetchone()
                return row['cnt'] >= config.RATE_LIMIT_PER_MIN
        except Exception as e:
            logger.error(f"频率限制检查失败: {e}")
            return False  # 出错时放行，避免误拦截

    def _log_request(self, ip: str, action: str):
        """记录请求到 rate_limit_log，并定期清理旧记录"""
        try:
            with db() as conn:
                conn.execute(
                    "INSERT INTO rate_limit_log (ip, action) VALUES (?, ?)",
                    (ip, action)
                )
                # 每 1000 条记录清理一次过期数据（避免表无限增长）
                row = conn.execute("SELECT COUNT(*) as cnt FROM rate_limit_log").fetchone()
                if row['cnt'] > 10000:
                    cutoff = (
                        datetime.now(timezone.utc) - timedelta(seconds=config.RATE_LIMIT_WINDOW * 2)
                    ).isoformat()
                    conn.execute(
                        "DELETE FROM rate_limit_log WHERE timestamp < ?", (cutoff,)
                    )
        except Exception as e:
            logger.debug(f"记录频率日志失败（非致命）: {e}")
