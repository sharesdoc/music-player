"""
数据库模块
使用 Python 内置 sqlite3，WAL 模式支持并发读写
所有查询使用参数化，防 SQL 注入
表：users, songs, categories, play_history, download_history,
    comments, user_playlists, playlist_songs, user_backgrounds,
    token_blacklist, rate_limit_log
在线迁移：_migrate_db() 处理 file_hash / review_status / user_backgrounds / bg_* 等增量变更
"""
import sqlite3
import logging
from contextlib import contextmanager
from pathlib import Path
from app.config import config

logger = logging.getLogger(__name__)

# ── DDL：所有表的建表语句 ──────────────────────────────────────────────────

_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=OFF;

-- 用户表
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT    UNIQUE NOT NULL,
    username        TEXT    UNIQUE NOT NULL,
    password_hash   TEXT    NOT NULL,
    salt            TEXT    NOT NULL,
    created_at      DATETIME DEFAULT (datetime('now')),
    last_login      DATETIME,
    login_attempts  INTEGER DEFAULT 0,
    locked_until    DATETIME,
    is_admin        INTEGER DEFAULT 0,
    upload_count    INTEGER DEFAULT 0,
    download_credits INTEGER DEFAULT 0
);

-- 分类表
CREATE TABLE IF NOT EXISTS categories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    UNIQUE NOT NULL,
    description TEXT,
    created_by  INTEGER,
    is_system   INTEGER DEFAULT 0,
    play_count  INTEGER DEFAULT 0,
    song_count  INTEGER DEFAULT 0,
    created_at  DATETIME DEFAULT (datetime('now'))
);

-- 歌曲表
CREATE TABLE IF NOT EXISTS songs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    filename        TEXT    NOT NULL,
    title           TEXT,
    artist          TEXT,
    category_id     INTEGER,
    file_path       TEXT    NOT NULL,
    file_size       INTEGER DEFAULT 0,
    duration        INTEGER DEFAULT 0,
    play_count      INTEGER DEFAULT 0,
    download_count  INTEGER DEFAULT 0,
    uploaded_by     INTEGER,
    is_shared       INTEGER DEFAULT 1,
    is_system       INTEGER DEFAULT 0,
    created_at      DATETIME DEFAULT (datetime('now')),
    lyrics          TEXT,
    lyrics_synced   TEXT,
    file_hash       TEXT,   -- SHA-256 of file content, used for deduplication
    review_status   TEXT DEFAULT 'approved',  -- pending | approved | rejected
    review_notes    TEXT    -- 管理员审核意见
);

-- Token 黑名单（注销后加入）
CREATE TABLE IF NOT EXISTS token_blacklist (
    jti         TEXT    PRIMARY KEY,
    expired_at  DATETIME NOT NULL
);

-- 播放历史
CREATE TABLE IF NOT EXISTS play_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    song_id     INTEGER NOT NULL,
    played_at   DATETIME DEFAULT (datetime('now'))
);

-- 下载历史
CREATE TABLE IF NOT EXISTS download_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    song_id         INTEGER NOT NULL,
    downloaded_at   DATETIME DEFAULT (datetime('now'))
);

-- 评论表
CREATE TABLE IF NOT EXISTS comments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    song_id     INTEGER NOT NULL,
    content     TEXT    NOT NULL,
    created_at  DATETIME DEFAULT (datetime('now'))
);

-- 用户播放列表
CREATE TABLE IF NOT EXISTS user_playlists (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    name        TEXT    NOT NULL,
    is_shared   INTEGER DEFAULT 0,
    created_at  DATETIME DEFAULT (datetime('now'))
);

-- 播放列表歌曲
CREATE TABLE IF NOT EXISTS playlist_songs (
    playlist_id INTEGER NOT NULL,
    song_id     INTEGER NOT NULL,
    position    INTEGER DEFAULT 0,
    PRIMARY KEY (playlist_id, song_id)
);

-- 频率限制日志（滑动窗口）
CREATE TABLE IF NOT EXISTS rate_limit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ip          TEXT    NOT NULL,
    action      TEXT    NOT NULL,
    timestamp   DATETIME DEFAULT (datetime('now'))
);

-- 用户背景图
CREATE TABLE IF NOT EXISTS user_backgrounds (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    filename    TEXT    NOT NULL,
    file_path   TEXT    NOT NULL,
    sort_order  INTEGER DEFAULT 0,
    created_at  DATETIME DEFAULT (datetime('now'))
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_user_bg_uid      ON user_backgrounds(user_id);
CREATE INDEX IF NOT EXISTS idx_songs_artist     ON songs(artist);
CREATE INDEX IF NOT EXISTS idx_songs_category   ON songs(category_id);
CREATE INDEX IF NOT EXISTS idx_songs_play_count ON songs(play_count DESC);
-- idx_songs_hash 在 _migrate_db() 中创建，避免旧库无 file_hash 列时报错
CREATE INDEX IF NOT EXISTS idx_play_history_uid ON play_history(user_id);
CREATE INDEX IF NOT EXISTS idx_play_history_sid ON play_history(song_id);
CREATE INDEX IF NOT EXISTS idx_rate_limit_ip    ON rate_limit_log(ip, action);
CREATE INDEX IF NOT EXISTS idx_comments_song    ON comments(song_id);

-- 删除申请
CREATE TABLE IF NOT EXISTS delete_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    song_id     INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    reason      TEXT    NOT NULL,
    status      TEXT    DEFAULT 'pending',
    admin_notes TEXT,
    created_at  DATETIME DEFAULT (datetime('now')),
    resolved_at DATETIME
);
CREATE INDEX IF NOT EXISTS idx_del_req_status ON delete_requests(status);
"""


def get_db_path() -> str:
    """获取数据库文件路径（绝对路径）"""
    return str(Path(config.DATABASE_PATH).resolve())


def get_connection() -> sqlite3.Connection:
    """创建新的 SQLite 连接，启用 WAL + Row Factory"""
    db_path = get_db_path()
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=10000")
    return conn


@contextmanager
def db():
    """
    数据库上下文管理器，用于依赖注入
    用法:
        with db() as conn:
            conn.execute(...)
    """
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"数据库事务回滚: {e}")
        raise
    finally:
        conn.close()


def _migrate_db(conn: sqlite3.Connection):
    """
    在线迁移：为现有数据库补充缺失的列/索引
    - 必须在 executescript(_DDL) 之后调用（新库已含所有列）
    - 旧库没有 file_hash 列时，在此补充列和索引
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(songs)")}
    if 'file_hash' not in cols:
        conn.execute("ALTER TABLE songs ADD COLUMN file_hash TEXT")
        logger.info("数据库迁移: 已添加 songs.file_hash 列")
    if 'review_status' not in cols:
        conn.execute("ALTER TABLE songs ADD COLUMN review_status TEXT DEFAULT 'approved'")
        conn.execute("UPDATE songs SET review_status = 'approved' WHERE review_status IS NULL")
        logger.info("数据库迁移: 已添加 songs.review_status 列")
    if 'review_notes' not in cols:
        conn.execute("ALTER TABLE songs ADD COLUMN review_notes TEXT")
        logger.info("数据库迁移: 已添加 songs.review_notes 列")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_songs_hash ON songs(file_hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_songs_review ON songs(review_status)")

    # ── user_backgrounds 表 ──
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if 'user_backgrounds' not in tables:
        conn.execute("""CREATE TABLE IF NOT EXISTS user_backgrounds (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            filename TEXT NOT NULL, file_path TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0, created_at DATETIME DEFAULT (datetime('now')))""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_bg_uid ON user_backgrounds(user_id)")
        logger.info("数据库迁移: 已创建 user_backgrounds 表")

    # ── users 表: 背景设置字段 ──
    user_cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    if 'bg_mode' not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN bg_mode TEXT DEFAULT 'single'")
    if 'bg_interval' not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN bg_interval INTEGER DEFAULT 10")
    if 'bg_active_id' not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN bg_active_id INTEGER")

    # ── delete_requests 表 ──
    if 'delete_requests' not in tables:
        conn.execute("""CREATE TABLE IF NOT EXISTS delete_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT, song_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL, reason TEXT NOT NULL,
            status TEXT DEFAULT 'pending', admin_notes TEXT,
            created_at DATETIME DEFAULT (datetime('now')), resolved_at DATETIME)""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_del_req_status ON delete_requests(status)")
        logger.info("数据库迁移: 已创建 delete_requests 表")


def init_db():
    """初始化数据库，创建所有表和索引，并执行在线迁移"""
    Path(config.DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
    logger.info(f"初始化数据库: {get_db_path()}")
    conn = get_connection()
    try:
        conn.executescript(_DDL)
        _migrate_db(conn)
        conn.commit()
        logger.info("✓ 数据库初始化完成")
    finally:
        conn.close()


def row_to_dict(row: sqlite3.Row) -> dict:
    """将 sqlite3.Row 转换为普通字典"""
    return dict(row) if row else None


def rows_to_list(rows) -> list:
    """将 sqlite3.Row 列表转换为字典列表"""
    return [dict(r) for r in rows] if rows else []
