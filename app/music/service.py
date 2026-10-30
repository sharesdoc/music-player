"""
音乐服务模块
- 歌曲 CRUD、流式播放、下载配额管理
- 分类、歌手、热门排行（仅统计公开歌曲：is_system=1 OR review_status='approved'）
- 上传文件校验（Magic Bytes + 扩展名 + SHA-256 内容去重）
- 审核系统：用户上传默认 pending，管理员审核后 approved/rejected
- 用户背景图管理：上传/删除/设置（单张/轮播）
"""
import hashlib
import logging
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Optional, List, Tuple

from app.config import config
from app.database import db, row_to_dict, rows_to_list

logger = logging.getLogger(__name__)


# ── 文件内容哈希（用于全局唯一性校验）────────────────────────────────────────
def compute_file_hash(file_bytes: bytes) -> str:
    """
    计算文件内容的 SHA-256 哈希值（64位十六进制字符串）
    用于上传去重：相同内容的文件只记录一次
    """
    return hashlib.sha256(file_bytes).hexdigest()


def find_song_by_hash(file_hash: str):
    """
    按文件内容哈希查找已存在的歌曲
    返回已存在的 song dict，若不存在返回 None
    """
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM songs WHERE file_hash = ? LIMIT 1",
            (file_hash,)
        ).fetchone()
        return row_to_dict(row)


# ── 文件格式 Magic Bytes 表 ────────────────────────────────────────────────
# MP3 单独用 _check_mp3_magic 处理（MPEG 同步字有多种变体）
_MAGIC_BYTES = {
    'm4a':  [(4, b'ftyp')],   # offset 4 处有 ftyp box
    'flac': [(0, b'fLaC')],
    'ogg':  [(0, b'OggS')],
    'wav':  [(0, b'RIFF')],
}


def _check_mp3_magic(file_bytes: bytes) -> bool:
    """
    MPEG 音频同步字校验：
    - ID3v2 标签头（ID3 开头）
    - 任何合法的 MPEG 帧头：第一字节 0xFF，第二字节高5位全1（0xE0 mask）
      覆盖：0xFF 0xFB(MPEG1 L3), 0xFF 0xFA(MPEG1 L2), 0xFF 0xF3, 0xFF 0xF2,
             0xFF 0xE3, 0xFF 0xFE 等所有变体
    """
    if len(file_bytes) < 2:
        return False
    if file_bytes[:3] == b'ID3':
        return True
    return file_bytes[0] == 0xFF and (file_bytes[1] & 0xE0) == 0xE0


def validate_audio_file(filename: str, file_bytes: bytes) -> Tuple[bool, str]:
    """
    双重校验：扩展名 + Magic Bytes / MPEG 同步字
    返回 (是否合法, 错误信息)
    """
    ext = Path(filename).suffix.lstrip('.').lower()
    if ext not in config.ALLOWED_EXTENSIONS:
        return False, f"不支持的文件格式 .{ext}，允许: {', '.join(config.ALLOWED_EXTENSIONS)}"

    if len(file_bytes) == 0:
        return False, "文件为空"

    file_size_mb = len(file_bytes) / (1024 * 1024)
    if file_size_mb > config.MAX_FILE_SIZE_MB:
        return False, f"文件过大 ({file_size_mb:.1f}MB)，最大允许 {config.MAX_FILE_SIZE_MB}MB"

    # MP3 用专用的 MPEG 同步字校验
    if ext == 'mp3':
        if not _check_mp3_magic(file_bytes):
            return False, "文件内容与扩展名 .mp3 不符，非有效 MPEG 音频文件"
        return True, ""

    # 其他格式用固定 Magic Bytes 表校验
    magic_list = _MAGIC_BYTES.get(ext, [])
    if magic_list:
        matched = any(
            file_bytes[offset:offset + len(magic)] == magic
            for offset, magic in magic_list
        )
        if not matched:
            return False, f"文件内容与扩展名 .{ext} 不符，疑似非音频文件"

    return True, ""


# ── 分类管理 ──────────────────────────────────────────────────────────────

def get_all_categories() -> List[dict]:
    """获取有歌曲的分类（空分类不显示），song_count 仅计公开可见歌曲"""
    with db() as conn:
        rows = conn.execute(
            """SELECT c.*,
                      (SELECT COUNT(*) FROM songs s
                       WHERE s.category_id = c.id
                         AND (s.is_system=1 OR s.review_status='approved')
                      ) AS song_count
               FROM categories c
               WHERE (SELECT COUNT(*) FROM songs s2
                      WHERE s2.category_id = c.id
                        AND (s2.is_system=1 OR s2.review_status='approved')) > 0
               ORDER BY c.is_system DESC, c.play_count DESC"""
        ).fetchall()
        return rows_to_list(rows)


def get_or_create_category(name: str, is_system: bool = False, created_by: Optional[int] = None) -> dict:
    """获取分类，不存在则创建"""
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM categories WHERE name = ?", (name,)
        ).fetchone()
        if row:
            return row_to_dict(row)
        cursor = conn.execute(
            "INSERT INTO categories (name, is_system, created_by) VALUES (?, ?, ?)",
            (name, 1 if is_system else 0, created_by)
        )
        return row_to_dict(conn.execute(
            "SELECT * FROM categories WHERE id = ?", (cursor.lastrowid,)
        ).fetchone())


def create_user_category(name: str, description: str, user_id: int) -> dict:
    """用户创建自定义分类"""
    with db() as conn:
        existing = conn.execute(
            "SELECT * FROM categories WHERE name = ?", (name,)
        ).fetchone()
        if existing:
            raise ValueError(f"分类 '{name}' 已存在")
        cursor = conn.execute(
            "INSERT INTO categories (name, description, created_by, is_system) VALUES (?, ?, ?, 0)",
            (name, description, user_id)
        )
        return row_to_dict(conn.execute(
            "SELECT * FROM categories WHERE id = ?", (cursor.lastrowid,)
        ).fetchone())


def update_category_counts():
    """更新所有分类的 song_count 和 play_count，仅计公开可见歌曲"""
    with db() as conn:
        # song_count 只统计已审核或系统歌曲，与 list_songs() 可见性保持一致
        conn.execute("""
            UPDATE categories SET song_count = (
                SELECT COUNT(*) FROM songs
                WHERE songs.category_id = categories.id
                  AND (songs.is_system=1 OR songs.review_status='approved')
            )
        """)
        conn.execute("""
            UPDATE categories SET play_count = (
                SELECT COALESCE(SUM(play_count), 0) FROM songs
                WHERE songs.category_id = categories.id
                  AND (songs.is_system=1 OR songs.review_status='approved')
            )
        """)


# ── 歌曲基础操作 ──────────────────────────────────────────────────────────

def _parse_filename(filename: str) -> Tuple[str, str]:
    """
    从文件名解析歌手和标题
    支持格式：「歌手 - 歌曲名.mp3」或「歌曲名.mp3」
    """
    name = Path(filename).stem
    # 去除常见后缀标注，如 (Acoustic Ver.), [Live], - instrumental 等
    name = re.sub(r'\s*[\(\[（【][^\)\]）】]*[\)\]）】]', '', name)
    name = re.sub(r'\s*-\s*(instrumental|live|remix|cover|version|feat\..*)$', '', name, flags=re.IGNORECASE)

    if ' - ' in name:
        parts = name.split(' - ', 1)
        return parts[0].strip(), parts[1].strip()
    # 兼容中文破折号「—」
    if '—' in name:
        parts = name.split('—', 1)
        return parts[0].strip(), parts[1].strip()
    return '', name.strip()


def get_song_by_id(song_id: int) -> Optional[dict]:
    with db() as conn:
        row = conn.execute("SELECT * FROM songs WHERE id = ?", (song_id,)).fetchone()
        return row_to_dict(row)


def list_songs(
    category_id: Optional[int] = None,
    artist: Optional[str] = None,
    query: Optional[str] = None,
    page: int = 1,
    limit: int = 30,
    sort: str = 'play_count',
    current_user_id: Optional[int] = None,
) -> dict:
    """
    分页查询歌曲列表
    sort: play_count | created_at | title
    可见性规则: is_system=1 的系统歌曲始终可见；用户上传歌曲仅在 approved 后公开，
    上传者可看到自己所有状态的歌曲。
    """
    offset = (page - 1) * limit
    conditions = []
    params = []

    # 可见性过滤：系统歌曲 OR 已审核 OR 本人上传（加 s. 前缀避免 JOIN 时列名歧义）
    if current_user_id:
        conditions.append(
            "(s.is_system=1 OR s.review_status='approved' OR s.uploaded_by=?)"
        )
        params.append(current_user_id)
    else:
        conditions.append("(s.is_system=1 OR s.review_status='approved')")

    if category_id:
        conditions.append("s.category_id = ?")
        params.append(category_id)
    if artist:
        conditions.append("s.artist LIKE ?")
        params.append(f"%{artist}%")
    if query:
        conditions.append("(s.title LIKE ? OR s.artist LIKE ? OR s.filename LIKE ?)")
        params.extend([f"%{query}%"] * 3)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    valid_sorts = {'play_count', 'created_at', 'title', 'download_count'}
    sort_col = sort if sort in valid_sorts else 'play_count'
    order = "DESC" if sort_col != 'title' else "ASC"

    with db() as conn:
        # COUNT 也用 s 别名，保持 where 子句一致
        total = conn.execute(
            f"SELECT COUNT(*) as cnt FROM songs s {where}", params
        ).fetchone()['cnt']

        rows = conn.execute(
            f"SELECT s.*, c.name as category_name FROM songs s "
            f"LEFT JOIN categories c ON s.category_id = c.id "
            f"{where} ORDER BY s.{sort_col} {order} LIMIT ? OFFSET ?",
            params + [limit, offset]
        ).fetchall()

    return {
        'total': total,
        'page': page,
        'limit': limit,
        'pages': (total + limit - 1) // limit,
        'songs': rows_to_list(rows),
    }


def list_artists(limit: int = 50) -> List[dict]:
    """获取热门歌手列表（按总播放量排序），只统计公开可见歌曲"""
    with db() as conn:
        rows = conn.execute(
            """SELECT artist, COUNT(*) as song_count, SUM(play_count) as total_plays
               FROM songs
               WHERE artist != '' AND artist IS NOT NULL
                 AND (is_system=1 OR review_status='approved')
               GROUP BY artist ORDER BY total_plays DESC LIMIT ?""",
            (limit,)
        ).fetchall()
        return rows_to_list(rows)


def get_hot_songs(limit: int = 20) -> List[dict]:
    """热门歌曲榜：只展示公开可见歌曲"""
    with db() as conn:
        rows = conn.execute(
            """SELECT s.*, c.name as category_name FROM songs s
               LEFT JOIN categories c ON s.category_id = c.id
               WHERE s.is_system=1 OR s.review_status='approved'
               ORDER BY s.play_count DESC LIMIT ?""",
            (limit,)
        ).fetchall()
        return rows_to_list(rows)


def get_hot_categories(limit: int = 10) -> List[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM categories ORDER BY play_count DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return rows_to_list(rows)


def get_hot_artists(limit: int = 10) -> List[dict]:
    return list_artists(limit)


# ── 播放记录 ──────────────────────────────────────────────────────────────

def record_play(song_id: int, user_id: Optional[int]):
    """记录一次播放，更新播放计数"""
    with db() as conn:
        conn.execute("UPDATE songs SET play_count = play_count + 1 WHERE id = ?", (song_id,))
        if user_id:
            conn.execute(
                "INSERT INTO play_history (user_id, song_id) VALUES (?, ?)",
                (user_id, song_id)
            )
    # 同步更新分类播放计数
    with db() as conn:
        song = conn.execute("SELECT category_id FROM songs WHERE id = ?", (song_id,)).fetchone()
        if song and song['category_id']:
            conn.execute(
                "UPDATE categories SET play_count = play_count + 1 WHERE id = ?",
                (song['category_id'],)
            )


def get_user_history(user_id: int, limit: int = 50) -> List[dict]:
    """获取用户播放历史（最近N首，去重）"""
    with db() as conn:
        rows = conn.execute(
            """SELECT DISTINCT s.*, ph.played_at, c.name as category_name
               FROM play_history ph
               JOIN songs s ON ph.song_id = s.id
               LEFT JOIN categories c ON s.category_id = c.id
               WHERE ph.user_id = ?
               ORDER BY ph.played_at DESC LIMIT ?""",
            (user_id, limit)
        ).fetchall()
        return rows_to_list(rows)


# ── 上传与下载 ────────────────────────────────────────────────────────────

def get_user_credits(user_id: int) -> dict:
    """获取用户的上传/下载配额信息"""
    with db() as conn:
        user = conn.execute(
            "SELECT upload_count, download_credits FROM users WHERE id = ?",
            (user_id,)
        ).fetchone()
        if not user:
            return {}
        used_downloads = conn.execute(
            "SELECT COUNT(*) as cnt FROM download_history WHERE user_id = ?",
            (user_id,)
        ).fetchone()['cnt']
        return {
            'upload_count': user['upload_count'],
            'download_credits': user['download_credits'],
            'downloads_used': used_downloads,
            'downloads_remaining': max(0, user['download_credits'] - used_downloads),
        }


def can_download(user_id: int, song_id: int) -> Tuple[bool, str]:
    """
    检查用户是否有权下载指定歌曲
    规则：
    1. 用户自己上传的文件 → 可下载
    2. 剩余下载配额 > 0 → 可下载（消耗1配额）
    3. 否则不可下载
    """
    with db() as conn:
        song = conn.execute("SELECT * FROM songs WHERE id = ?", (song_id,)).fetchone()
        if not song:
            return False, "歌曲不存在"
        # 自己上传的，始终可下载
        if song['uploaded_by'] == user_id:
            return True, "own"
        # 检查配额
        credits = get_user_credits(user_id)
        if credits.get('downloads_remaining', 0) > 0:
            return True, "credit"
        return False, f"下载配额不足，还需上传 {1 - credits.get('downloads_remaining', 0)} 首歌才能下载"


def record_download(user_id: int, song_id: int) -> bool:
    """
    记录下载，消耗配额
    如果是自己上传的文件，不消耗配额
    返回是否消耗了配额
    """
    with db() as conn:
        song = conn.execute("SELECT uploaded_by FROM songs WHERE id = ?", (song_id,)).fetchone()
        if not song:
            return False

        conn.execute(
            "INSERT INTO download_history (user_id, song_id) VALUES (?, ?)",
            (user_id, song_id)
        )
        conn.execute(
            "UPDATE songs SET download_count = download_count + 1 WHERE id = ?",
            (song_id,)
        )
        # 只有下载他人文件才算消耗配额
        return song['uploaded_by'] != user_id


def save_uploaded_file(
    filename: str,
    file_bytes: bytes,
    user_id: int,
    category_id: Optional[int] = None,
) -> dict:
    """
    保存用户上传的音频文件
    - 全局唯一性校验（SHA-256 文件内容哈希）：已存在的文件不重复入库，不计入上传配额
    - 格式校验（Magic Bytes + 扩展名 + 大小）
    - 保存到 uploads/{uuid}.ext
    - 写入数据库，更新用户上传计数和下载配额
    """
    # ① 格式校验（先做，快速拦截非法文件）
    valid, err_msg = validate_audio_file(filename, file_bytes)
    if not valid:
        raise ValueError(err_msg)

    # ② 计算文件内容哈希，全局去重
    file_hash = compute_file_hash(file_bytes)
    existing = find_song_by_hash(file_hash)
    if existing:
        logger.warning(
            f"上传拒绝（重复文件）: user={user_id}, hash={file_hash[:16]}…, "
            f"existing_song_id={existing['id']}, existing_file={existing['filename']}"
        )
        raise ValueError(
            f"该文件内容已存在于系统中（与《{existing.get('title') or existing['filename']}》相同），"
            f"不计入上传数量，也无法获得下载配额"
        )

    # ③ 校验用户上传配额上限
    with db() as conn:
        user = conn.execute(
            "SELECT upload_count FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if user and user['upload_count'] >= config.MAX_UPLOAD_PER_USER:
            raise ValueError(f"已达到上传上限 {config.MAX_UPLOAD_PER_USER} 首")

    # ④ 写文件到磁盘
    ext = Path(filename).suffix.lower()
    unique_name = f"{uuid.uuid4().hex}{ext}"
    upload_dir = Path(config.UPLOAD_PATH)
    upload_dir.mkdir(parents=True, exist_ok=True)
    save_path = upload_dir / unique_name

    with open(save_path, 'wb') as f:
        f.write(file_bytes)

    # ⑤ 解析歌手/标题、读取时长
    artist, title = _parse_filename(filename)
    file_size = len(file_bytes)
    duration = _get_duration(str(save_path))

    # ⑥ 写数据库，更新用户上传计数 + 下载配额
    # admin (id=0) 上传直接 approved，无需自己审核自己
    _review = 'approved' if user_id == 0 else 'pending'
    _shared = 1 if user_id == 0 else 0
    with db() as conn:
        cursor = conn.execute(
            """INSERT INTO songs
               (filename, title, artist, category_id, file_path, file_size,
                duration, uploaded_by, is_shared, is_system, file_hash, review_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
            (filename, title, artist, category_id, str(save_path),
             file_size, duration, user_id, _shared, file_hash, _review)
        )
        song_id = cursor.lastrowid
        conn.execute(
            """UPDATE users
               SET upload_count = upload_count + 1,
                   download_credits = download_credits + 1
               WHERE id = ?""",
            (user_id,)
        )
        logger.info(
            f"文件上传成功: user={user_id}, song_id={song_id}, "
            f"file={filename}, hash={file_hash[:16]}…"
        )
        return row_to_dict(conn.execute(
            "SELECT * FROM songs WHERE id = ?", (song_id,)
        ).fetchone())


def delete_song(song_id: int, user_id: int, reason: str = '') -> dict:
    """
    删除歌曲（权限规则）：
    - 系统歌曲：不可删除
    - 只能操作自己上传的歌曲
    - pending/rejected 歌曲：直接删除
    - approved 歌曲：提交删除申请，等待管理员审核
    """
    with db() as conn:
        song = conn.execute("SELECT * FROM songs WHERE id = ?", (song_id,)).fetchone()
        if not song:
            raise ValueError("歌曲不存在")
        song = row_to_dict(song)

        if song['is_system']:
            raise ValueError("系统歌曲不可删除")
        if song['uploaded_by'] != user_id:
            raise ValueError("无权删除：只能删除自己上传的歌曲")

        # approved 歌曲：提交删除申请
        if song['review_status'] == 'approved':
            if not reason.strip():
                raise ValueError("已审核通过的歌曲需要填写删除理由")
            # 检查是否已有 pending 申请
            existing = conn.execute(
                "SELECT id FROM delete_requests WHERE song_id = ? AND user_id = ? AND status = 'pending'",
                (song_id, user_id)
            ).fetchone()
            if existing:
                raise ValueError("已有待审核的删除申请")
            conn.execute(
                "INSERT INTO delete_requests (song_id, user_id, reason) VALUES (?, ?, ?)",
                (song_id, user_id, reason.strip())
            )
            logger.info(f"删除申请已提交: song_id={song_id}, user={user_id}")
            return {'action': 'requested', 'message': '删除申请已提交，等待管理员审核'}

        # pending/rejected 歌曲：直接删除
        _do_delete_song(conn, song)
        logger.info(f"歌曲已删除: song_id={song_id}, by_user={user_id}")
        return {'action': 'deleted', 'message': '删除成功'}


def _do_delete_song(conn, song: dict):
    """执行歌曲删除（物理文件+数据库记录）"""
    file_path = song['file_path']
    upload_path_abs = str(Path(config.UPLOAD_PATH).resolve())
    file_abs = str(Path(file_path).resolve())
    if file_abs.startswith(upload_path_abs) and os.path.exists(file_abs):
        os.remove(file_abs)
        logger.info(f"已删除文件: {file_abs}")

    conn.execute("DELETE FROM songs WHERE id = ?", (song['id'],))
    conn.execute("DELETE FROM delete_requests WHERE song_id = ?", (song['id'],))
    if song.get('uploaded_by'):
        conn.execute(
            "UPDATE users SET upload_count = MAX(0, upload_count - 1) WHERE id = ?",
            (song['uploaded_by'],)
        )


def share_song(song_id: int, user_id: int) -> bool:
    """分享歌曲（设为公开可下载）"""
    with db() as conn:
        song = conn.execute("SELECT * FROM songs WHERE id = ?", (song_id,)).fetchone()
        if not song:
            raise ValueError("歌曲不存在")
        if song['uploaded_by'] != user_id:
            raise ValueError("只能分享自己上传的歌曲")
        conn.execute("UPDATE songs SET is_shared = 1 WHERE id = ?", (song_id,))
        return True


# ── 评论 ──────────────────────────────────────────────────────────────────

def get_comments(song_id: int, page: int = 1, limit: int = 20) -> dict:
    offset = (page - 1) * limit
    with db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) as cnt FROM comments WHERE song_id = ?", (song_id,)
        ).fetchone()['cnt']
        rows = conn.execute(
            """SELECT c.*, u.username FROM comments c
               JOIN users u ON c.user_id = u.id
               WHERE c.song_id = ?
               ORDER BY c.created_at DESC LIMIT ? OFFSET ?""",
            (song_id, limit, offset)
        ).fetchall()
    return {'total': total, 'comments': rows_to_list(rows)}


def add_comment(song_id: int, user_id: int, content: str) -> dict:
    content = content.strip()
    if not content or len(content) > 500:
        raise ValueError("评论内容不能为空且不能超过500字")
    with db() as conn:
        cursor = conn.execute(
            "INSERT INTO comments (song_id, user_id, content) VALUES (?, ?, ?)",
            (song_id, user_id, content)
        )
        return row_to_dict(conn.execute(
            """SELECT c.*, u.username FROM comments c
               JOIN users u ON c.user_id = u.id WHERE c.id = ?""",
            (cursor.lastrowid,)
        ).fetchone())


# ── 播放列表 ──────────────────────────────────────────────────────────────

def get_user_playlists(user_id: int) -> List[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM user_playlists WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,)
        ).fetchall()
        return rows_to_list(rows)


def create_playlist(user_id: int, name: str) -> dict:
    name = name.strip()
    if not name or len(name) > 50:
        raise ValueError("播放列表名称无效（最多50字）")
    with db() as conn:
        cursor = conn.execute(
            "INSERT INTO user_playlists (user_id, name) VALUES (?, ?)",
            (user_id, name)
        )
        return row_to_dict(conn.execute(
            "SELECT * FROM user_playlists WHERE id = ?", (cursor.lastrowid,)
        ).fetchone())


def add_song_to_playlist(playlist_id: int, song_id: int, user_id: int):
    with db() as conn:
        pl = conn.execute(
            "SELECT * FROM user_playlists WHERE id = ? AND user_id = ?",
            (playlist_id, user_id)
        ).fetchone()
        if not pl:
            raise ValueError("播放列表不存在或无权操作")
        conn.execute(
            "INSERT OR IGNORE INTO playlist_songs (playlist_id, song_id) VALUES (?, ?)",
            (playlist_id, song_id)
        )


def get_playlist_songs(playlist_id: int) -> List[dict]:
    with db() as conn:
        rows = conn.execute(
            """SELECT s.*, c.name as category_name FROM playlist_songs ps
               JOIN songs s ON ps.song_id = s.id
               LEFT JOIN categories c ON s.category_id = c.id
               WHERE ps.playlist_id = ? ORDER BY ps.position""",
            (playlist_id,)
        ).fetchall()
        return rows_to_list(rows)


# ── 歌词 ──────────────────────────────────────────────────────────────────

def get_song_lyrics(song_id: int) -> Optional[dict]:
    with db() as conn:
        row = conn.execute(
            "SELECT lyrics, lyrics_synced FROM songs WHERE id = ?", (song_id,)
        ).fetchone()
        return row_to_dict(row) if row else None


def save_lyrics(song_id: int, lyrics: str, lyrics_synced: Optional[str] = None):
    with db() as conn:
        conn.execute(
            "UPDATE songs SET lyrics = ?, lyrics_synced = ? WHERE id = ?",
            (lyrics, lyrics_synced, song_id)
        )


# ── 辅助工具 ──────────────────────────────────────────────────────────────

def _get_duration(file_path: str) -> int:
    """尝试用 mutagen 读取音频时长（秒），失败返回0"""
    try:
        from mutagen import File
        audio = File(file_path)
        if audio and audio.info:
            return int(audio.info.length)
    except Exception:
        pass
    return 0


def get_file_path_secure(song_id: int) -> Optional[str]:
    """
    安全获取歌曲文件路径
    防路径穿越：验证路径在允许目录内
    """
    with db() as conn:
        row = conn.execute(
            "SELECT file_path, is_system FROM songs WHERE id = ?", (song_id,)
        ).fetchone()
    if not row:
        return None

    file_path = str(Path(row['file_path']).resolve())
    # 允许的根目录
    allowed_roots = [
        str(Path(config.UPLOAD_PATH).resolve()),
        str(Path(config.SYSTEM_MUSIC_PATH).resolve()),
    ]
    # 本地测试路径（env.loc 可能指向不同路径）
    for root in allowed_roots:
        if file_path.startswith(root):
            return file_path if os.path.exists(file_path) else None

    logger.warning(f"可疑路径访问被拦截: {file_path}")
    return None


# ── 背景图管理 ──────────────────────────────────────────────────────────────

_BG_MAGIC = {
    'jpg':  [(0, b'\xff\xd8\xff')],
    'jpeg': [(0, b'\xff\xd8\xff')],
    'png':  [(0, b'\x89PNG')],
    'webp': [(8, b'WEBP')],
    'gif':  [(0, b'GIF8')],
}


def validate_image_file(filename: str, file_bytes: bytes) -> Tuple[bool, str]:
    ext = Path(filename).suffix.lstrip('.').lower()
    if ext not in config.BG_ALLOWED_EXTENSIONS:
        return False, f"不支持的图片格式 .{ext}"
    if not file_bytes:
        return False, "文件为空"
    size_mb = len(file_bytes) / (1024 * 1024)
    if size_mb > config.BG_MAX_FILE_SIZE_MB:
        return False, f"图片过大 ({size_mb:.1f}MB)，最大 {config.BG_MAX_FILE_SIZE_MB}MB"
    for offset, magic in _BG_MAGIC.get(ext, []):
        if file_bytes[offset:offset + len(magic)] != magic:
            return False, f"文件内容与 .{ext} 不符"
    return True, ""


def save_background_image(filename: str, file_bytes: bytes, user_id: int) -> dict:
    valid, err = validate_image_file(filename, file_bytes)
    if not valid:
        raise ValueError(err)

    with db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM user_backgrounds WHERE user_id = ?", (user_id,)
        ).fetchone()[0]
        if count >= config.BG_MAX_PER_USER:
            raise ValueError(f"最多只能上传 {config.BG_MAX_PER_USER} 张背景图")

    ext = Path(filename).suffix.lower()
    unique_name = f"{uuid.uuid4().hex}{ext}"
    bg_dir = Path(config.BG_UPLOAD_PATH)
    bg_dir.mkdir(parents=True, exist_ok=True)
    save_path = bg_dir / unique_name

    with open(save_path, 'wb') as f:
        f.write(file_bytes)

    with db() as conn:
        cursor = conn.execute(
            "INSERT INTO user_backgrounds (user_id, filename, file_path, sort_order) VALUES (?, ?, ?, ?)",
            (user_id, filename, str(save_path), count)
        )
        row = conn.execute("SELECT * FROM user_backgrounds WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return row_to_dict(row)


def get_user_backgrounds(user_id: int) -> dict:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM user_backgrounds WHERE user_id = ? ORDER BY sort_order", (user_id,)
        ).fetchall()
        user = conn.execute(
            "SELECT bg_mode, bg_interval, bg_active_id FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        settings = {
            'mode': (user['bg_mode'] or 'single') if user else 'single',
            'interval': (user['bg_interval'] or 10) if user else 10,
            'active_id': user['bg_active_id'] if user else None,
        }
        return {'backgrounds': rows_to_list(rows), 'settings': settings}


def delete_background(bg_id: int, user_id: int):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM user_backgrounds WHERE id = ? AND user_id = ?", (bg_id, user_id)
        ).fetchone()
        if not row:
            raise ValueError("背景图不存在")
        conn.execute("DELETE FROM user_backgrounds WHERE id = ?", (bg_id,))
        conn.execute(
            "UPDATE users SET bg_active_id = NULL WHERE id = ? AND bg_active_id = ?",
            (user_id, bg_id)
        )
    try:
        Path(row['file_path']).unlink(missing_ok=True)
    except Exception:
        logger.warning(f"背景图文件删除失败: {row['file_path']}")


def update_bg_settings(user_id: int, mode: str, interval: int, active_id):
    with db() as conn:
        conn.execute(
            "UPDATE users SET bg_mode = ?, bg_interval = ?, bg_active_id = ? WHERE id = ?",
            (mode, interval, active_id, user_id)
        )


def get_background_path(bg_id: int, user_id: int) -> Optional[str]:
    with db() as conn:
        row = conn.execute(
            "SELECT file_path FROM user_backgrounds WHERE id = ? AND user_id = ?",
            (bg_id, user_id)
        ).fetchone()
        if not row:
            return None
        p = Path(row['file_path'])
        return str(p) if p.exists() else None
