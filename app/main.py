"""
NEUROMIX // GRID - FastAPI 主入口
挂载路由（auth/music/admin），配置中间件（CORS/频率限制/安全头），
提供健康检查、静态文件服务（SPA单页应用）
后台定时任务：每小时刷新热门排行统计（仅计公开歌曲）
"""
import asyncio
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import config
from app.database import init_db
from app.auth.router import router as auth_router
from app.music.router import router as music_router
from app.admin.router import router as admin_router
from app.middleware.rate_limit import RateLimitMiddleware

# ── 日志配置 ──────────────────────────────────────────────────────────────
log_dir = Path("log")
log_dir.mkdir(exist_ok=True)

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(log_dir / "app.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger(__name__)

# ── FastAPI 应用 ──────────────────────────────────────────────────────────
app = FastAPI(
    title=config.APP_NAME,
    version="1.0.0",
    description="赛博朋克音乐共享平台 - 贡献越多获取越多",
    docs_url="/docs" if config.DEBUG else None,  # 生产环境关闭文档
    redoc_url=None,
)

# ── 中间件（顺序很重要：从外到内） ──────────────────────────────────────

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 频率限制
app.add_middleware(RateLimitMiddleware)


# 安全响应头
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


# 全局异常处理
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"未处理异常: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "服务器内部错误，请稍后重试"}
    )


# ── 路由 ──────────────────────────────────────────────────────────────────
app.include_router(auth_router)
app.include_router(music_router)
app.include_router(admin_router)

# ── 静态文件 ──────────────────────────────────────────────────────────────
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)

# 单页应用：所有非 API 路由返回 index.html
@app.get("/")
async def serve_index():
    index_file = static_dir / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return JSONResponse({"message": f"{config.APP_NAME} API 服务运行中"})


@app.get("/health")
async def health_check():
    """健康检查端点（用于负载均衡、监控）"""
    return {
        "status": "ok",
        "app": config.APP_NAME,
        "env": config.APP_ENV,
        "version": "1.0.0",
    }


# ── 后台定时任务 ──────────────────────────────────────────────────────────

async def _hourly_ranking_task():
    """
    后台任务：每小时刷新热门排行统计
    - 更新所有分类的 song_count / play_count 聚合值
    - 避免累积误差（直接重新统计，不用增量）
    """
    await asyncio.sleep(60)  # 启动后延迟60秒再开始首次刷新
    while True:
        try:
            from app.music.service import update_category_counts
            update_category_counts()
            logger.info("热门排行已刷新")
        except Exception as e:
            logger.error(f"排行刷新失败: {e}", exc_info=True)
        await asyncio.sleep(3600)  # 每小时执行一次


# ── 启动事件 ──────────────────────────────────────────────────────────────
@app.on_event("startup")
async def on_startup():
    """应用启动时初始化数据库、校验配置、启动后台任务"""
    logger.info(f"🚀 {config.APP_NAME} 启动中...")
    config.validate()
    init_db()
    # 启动后台定时排行刷新任务
    asyncio.create_task(_hourly_ranking_task())
    logger.info(f"✓ 服务已就绪，监听 {config.HOST}:{config.PORT}")
    logger.info(f"  环境: {config.APP_ENV}")
    logger.info(f"  数据库: {config.DATABASE_PATH}")
    logger.info(f"  音乐库: {config.SYSTEM_MUSIC_PATH}")
    logger.info(f"  上传目录: {config.UPLOAD_PATH}")
    logger.info(f"  AI功能: {'开启' if config.AI_ENABLED else '关闭'}")


@app.on_event("shutdown")
async def on_shutdown():
    logger.info(f"👋 {config.APP_NAME} 已停止")


# ── 本地运行入口 ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=config.HOST,
        port=config.PORT,
        reload=config.DEBUG,
        log_level=config.LOG_LEVEL.lower(),
    )
