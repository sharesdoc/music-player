"""
配置加载器
优先级：环境变量 > env文件 > config.yml
支持本地(env.loc)和生产(env.pro)双环境
配置项：应用基础、音乐路径、安全/JWT、数据库、AI、背景图、管理员、CORS
"""
import os
import yaml
import logging
from pathlib import Path

# 项目根目录
BASE_DIR = Path(__file__).parent.parent

logger = logging.getLogger(__name__)


def _load_env_file(env_file: Path) -> None:
    """加载 env 文件到环境变量（不覆盖已有的系统环境变量）"""
    if not env_file.exists():
        return
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            # 跳过注释和空行
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                key, _, value = line.partition('=')
                key = key.strip()
                value = value.strip()
                # 系统环境变量优先，不覆盖
                if key not in os.environ:
                    os.environ[key] = value


def _load_yml() -> dict:
    """加载 config.yml"""
    yml_path = BASE_DIR / 'config.yml'
    if yml_path.exists():
        with open(yml_path) as f:
            return yaml.safe_load(f) or {}
    return {}


# 按优先级加载 env 文件
_app_env = os.environ.get('APP_ENV', 'local')
if _app_env == 'production':
    _load_env_file(BASE_DIR / 'env.pro')
else:
    _load_env_file(BASE_DIR / 'env.loc')

_yml = _load_yml()


def _get(yml_path: str, env_key: str, default=None):
    """从 yml 取值，可被环境变量覆盖"""
    keys = yml_path.split('.')
    val = _yml
    for k in keys:
        if isinstance(val, dict):
            val = val.get(k)
        else:
            val = None
            break
    # 环境变量优先
    return os.environ.get(env_key, val if val is not None else default)


class Config:
    """全局配置对象，所有模块通过此类读取配置"""

    # ── 应用基础 ────────────────────────────────────────────────────────────
    APP_NAME: str = _get('app.name', 'APP_NAME', 'NEUROMIX // GRID')
    APP_ENV: str = os.environ.get('APP_ENV', 'local')
    DEBUG: bool = str(_get('app.debug', 'APP_DEBUG', 'false')).lower() == 'true'
    HOST: str = _get('app.host', 'APP_HOST', '0.0.0.0')
    PORT: int = int(_get('app.port', 'APP_PORT', 8080))
    LOG_LEVEL: str = _get('app.log_level', 'LOG_LEVEL', 'INFO').upper()

    # ── 音乐路径 ────────────────────────────────────────────────────────────
    SYSTEM_MUSIC_PATH: str = _get('music.system_music_path', 'SYSTEM_MUSIC_PATH', '/var/www/html/downloads/mp3')
    UPLOAD_PATH: str = _get('music.upload_path', 'UPLOAD_PATH', './uploads')
    ALLOWED_EXTENSIONS: list = _get('music.allowed_extensions', 'ALLOWED_EXTENSIONS', ['mp3', 'm4a', 'flac', 'ogg', 'wav'])
    MAX_FILE_SIZE_MB: int = int(_get('music.max_file_size_mb', 'MAX_FILE_SIZE_MB', 50))
    MAX_UPLOAD_PER_USER: int = int(_get('music.max_upload_per_user', 'MAX_UPLOAD_PER_USER', 1000))
    CHUNK_SIZE: int = int(_get('music.chunk_size_kb', 'CHUNK_SIZE_KB', 64)) * 1024

    # ── 背景图 ──────────────────────────────────────────────────────────
    BG_UPLOAD_PATH: str = _get('music.bg_upload_path', 'BG_UPLOAD_PATH', './uploads/backgrounds')
    BG_MAX_FILE_SIZE_MB: int = int(_get('music.bg_max_file_size_mb', 'BG_MAX_FILE_SIZE_MB', 20))
    BG_MAX_PER_USER: int = int(_get('music.bg_max_per_user', 'BG_MAX_PER_USER', 10))
    BG_ALLOWED_EXTENSIONS: list = ['jpg', 'jpeg', 'png', 'webp', 'gif']

    # ── 安全 ────────────────────────────────────────────────────────────────
    JWT_SECRET: str = os.environ.get('JWT_SECRET', 'INSECURE-CHANGE-ME')
    JWT_ALGORITHM: str = 'HS256'
    JWT_EXPIRE_MINUTES: int = int(_get('security.jwt_expire_minutes', 'JWT_EXPIRE_MINUTES', 60))
    REFRESH_EXPIRE_DAYS: int = int(_get('security.refresh_expire_days', 'REFRESH_EXPIRE_DAYS', 7))
    LOGIN_MAX_ATTEMPTS: int = int(_get('security.login_max_attempts', 'LOGIN_MAX_ATTEMPTS', 5))
    LOGIN_LOCKOUT_MINUTES: int = int(_get('security.login_lockout_minutes', 'LOGIN_LOCKOUT_MINUTES', 15))
    RATE_LIMIT_PER_MIN: int = int(_get('security.rate_limit_per_minute', 'RATE_LIMIT_PER_MIN', 100))
    RATE_LIMIT_WINDOW: int = int(_get('security.rate_limit_window_seconds', 'RATE_LIMIT_WINDOW', 60))

    # ── 数据库 ──────────────────────────────────────────────────────────────
    DATABASE_PATH: str = _get('database.path', 'DATABASE_PATH', './data/music.db')

    # ── AI（OpenAI 兼容接口）──────────────────────────────────────────────
    AI_BASE_URL: str = os.environ.get('AI_BASE_URL', 'https://api.openai.com/v1')
    AI_API_KEY: str = os.environ.get('AI_API_KEY', os.environ.get('ANTHROPIC_API_KEY', ''))
    AI_MODEL: str = _get('ai.model', 'AI_MODEL', 'gpt-4o-mini')
    AI_ENABLED: bool = str(os.environ.get('AI_ENABLED', _get('ai.classify_enabled', '', 'true'))).lower() == 'true'
    # 兼容旧配置
    ANTHROPIC_API_KEY: str = os.environ.get('ANTHROPIC_API_KEY', '')

    # ── 超级管理员（env 认证，不存入数据库） ────────────────────────────────
    ADMIN_USERNAME: str = os.environ.get('ADMIN_USERNAME', '')
    ADMIN_PASSWORD: str = os.environ.get('ADMIN_PASSWORD', '')

    # ── CORS ────────────────────────────────────────────────────────────────
    CORS_ORIGINS: list = ['*']

    @classmethod
    def validate(cls):
        """启动时校验关键配置"""
        if cls.JWT_SECRET in ('INSECURE-CHANGE-ME', ''):
            if cls.APP_ENV == 'production':
                raise ValueError("生产环境必须设置 JWT_SECRET 环境变量！")
            else:
                logger.warning("⚠️  JWT_SECRET 未设置，使用不安全的默认值（仅开发环境可接受）")

        if cls.AI_ENABLED and not cls.AI_API_KEY:
            logger.warning("⚠️  AI_ENABLED=true 但 AI_API_KEY 未设置，AI功能将被禁用")
            cls.AI_ENABLED = False

        # 确保路径存在
        Path(cls.DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
        Path(cls.UPLOAD_PATH).mkdir(parents=True, exist_ok=True)
        Path(cls.BG_UPLOAD_PATH).mkdir(parents=True, exist_ok=True)


config = Config()
