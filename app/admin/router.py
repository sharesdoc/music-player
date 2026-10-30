"""
管理后台路由模块
所有接口均需 require_admin 鉴权（env 超级管理员，JWT is_admin:true）

统计/审核/用户管理/日志 + 分类管理（重命名/合并/新建/删除）+ 歌曲共享控制
"""
import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth.router import require_admin
from app.database import db, rows_to_list, row_to_dict

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["管理后台"])


# ── Schema ────────────────────────────────────────────────────────────────────

class ReviewRequest(BaseModel):
    action: str = Field(pattern=r'^(approve|reject)$')
    notes: str = Field(default='', max_length=500)


class RenameCategoryRequest(BaseModel):
    name: str = Field(min_length=1, max_length=50)


class MergeCategoryRequest(BaseModel):
    target_id: int


class CreateCategoryRequest(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    description: str = Field(default='', max_length=200)


class DeleteCategoryRequest(BaseModel):
    delete_songs: bool = False


class ToggleShareRequest(BaseModel):
    shared: bool


class RenameSongRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    artist: str = Field(default='', max_length=200)


# ── 统计概览 ──────────────────────────────────────────────────────────────────

@router.get("/stats")
async def get_stats(_: dict = Depends(require_admin)):
    """返回平台核心统计数据"""
    with db() as conn:
        user_count = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()['c']
        song_total = conn.execute("SELECT COUNT(*) as c FROM songs").fetchone()['c']
        song_approved = conn.execute(
            "SELECT COUNT(*) as c FROM songs WHERE review_status='approved'"
        ).fetchone()['c']
        song_pending = conn.execute(
            "SELECT COUNT(*) as c FROM songs WHERE review_status='pending'"
        ).fetchone()['c']
        song_rejected = conn.execute(
            "SELECT COUNT(*) as c FROM songs WHERE review_status='rejected'"
        ).fetchone()['c']

        # 各分类歌曲数
        category_rows = conn.execute(
            """SELECT COALESCE(c.name,'未分类') as name, COUNT(s.id) as count
               FROM songs s LEFT JOIN categories c ON s.category_id = c.id
               GROUP BY s.category_id ORDER BY count DESC"""
        ).fetchall()

        # 用户维度统计（含各自歌曲数）
        user_rows = conn.execute(
            """SELECT u.id, u.username, u.email, u.upload_count,
                      u.download_credits, u.created_at,
                      COUNT(s.id) as song_count,
                      SUM(CASE WHEN s.review_status='pending' THEN 1 ELSE 0 END) as pending_count
               FROM users u LEFT JOIN songs s ON s.uploaded_by = u.id
               GROUP BY u.id ORDER BY u.upload_count DESC"""
        ).fetchall()

    return {
        'user_count': user_count,
        'song_total': song_total,
        'song_approved': song_approved,
        'song_pending': song_pending,
        'song_rejected': song_rejected,
        'categories': rows_to_list(category_rows),
        'users': rows_to_list(user_rows),
    }


# ── 歌曲管理 ──────────────────────────────────────────────────────────────────

@router.get("/songs")
async def admin_list_songs(
    status: Optional[str] = Query(None, description="pending|approved|rejected|all"),
    page: int = Query(1, ge=1),
    limit: int = Query(30, ge=1, le=100),
    _: dict = Depends(require_admin),
):
    """管理员查看所有歌曲，可按审核状态过滤"""
    offset = (page - 1) * limit
    where = ""
    params: list = []
    if status and status != 'all':
        where = "WHERE s.review_status = ?"
        params.append(status)

    with db() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) as c FROM songs s {where}", params
        ).fetchone()['c']
        rows = conn.execute(
            f"""SELECT s.*, c.name as category_name, u.username as uploader_name
                FROM songs s
                LEFT JOIN categories c ON s.category_id = c.id
                LEFT JOIN users u ON s.uploaded_by = u.id
                {where}
                ORDER BY s.created_at DESC LIMIT ? OFFSET ?""",
            params + [limit, offset]
        ).fetchall()

    return {
        'total': total,
        'page': page,
        'limit': limit,
        'pages': (total + limit - 1) // limit,
        'songs': rows_to_list(rows),
    }


@router.post("/songs/{song_id}/review")
async def review_song(
    song_id: int,
    req: ReviewRequest,
    background_tasks: BackgroundTasks,
    admin: dict = Depends(require_admin),
):
    """审核歌曲：approve 公开 / reject 拒绝。通过后后台自动 AI 分类"""
    with db() as conn:
        song = row_to_dict(
            conn.execute("SELECT * FROM songs WHERE id = ?", (song_id,)).fetchone()
        )
        if not song:
            raise HTTPException(status_code=404, detail="歌曲不存在")

        if req.action == 'approve':
            conn.execute(
                "UPDATE songs SET review_status='approved', is_shared=1, review_notes=? WHERE id=?",
                (req.notes, song_id)
            )
            logger.info(f"管理员审核通过: song_id={song_id}, admin={admin['username']}")
            if not song.get('category_id'):
                background_tasks.add_task(_auto_classify_song, song_id, song)
            return {'message': '已通过审核，歌曲已公开'}
        else:
            # 拒绝 → 自动删除文件和记录
            file_path = song.get('file_path', '')
            conn.execute("DELETE FROM songs WHERE id = ?", (song_id,))
            uploader_id = song.get('uploaded_by')
            if uploader_id:
                conn.execute(
                    "UPDATE users SET upload_count = MAX(0, upload_count - 1), "
                    "download_credits = MAX(0, download_credits - 1) WHERE id = ?",
                    (uploader_id,)
                )
            logger.info(f"管理员拒绝并删除: song_id={song_id}, admin={admin['username']}")
        # 删除物理文件
        import os
        from pathlib import Path
        from app.config import config
        upload_abs = str(Path(config.UPLOAD_PATH).resolve())
        try:
            file_abs = str(Path(file_path).resolve())
            if file_abs.startswith(upload_abs) and os.path.exists(file_abs):
                os.remove(file_abs)
        except Exception:
            pass
        return {'message': '已拒绝，文件已删除'}


# ── 用户管理 ──────────────────────────────────────────────────────────────────

@router.get("/users")
async def admin_list_users(
    page: int = Query(1, ge=1),
    limit: int = Query(30, ge=1, le=100),
    _: dict = Depends(require_admin),
):
    """获取用户列表（含上传/下载统计）"""
    offset = (page - 1) * limit
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()['c']
        rows = conn.execute(
            """SELECT u.id, u.username, u.email, u.upload_count, u.download_credits,
                      u.created_at, u.last_login, u.is_admin,
                      COUNT(s.id) as song_count,
                      SUM(s.play_count) as total_plays,
                      SUM(s.download_count) as total_downloads
               FROM users u LEFT JOIN songs s ON s.uploaded_by = u.id
               GROUP BY u.id ORDER BY u.created_at DESC LIMIT ? OFFSET ?""",
            (limit, offset)
        ).fetchall()
    return {
        'total': total,
        'page': page,
        'limit': limit,
        'pages': (total + limit - 1) // limit,
        'users': rows_to_list(rows),
    }


# ── 日志查询 ──────────────────────────────────────────────────────────────────

@router.get("/logs/upload")
async def admin_upload_logs(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    _: dict = Depends(require_admin),
):
    """上传日志：所有用户上传的歌曲记录"""
    offset = (page - 1) * limit
    with db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) as c FROM songs WHERE is_system=0"
        ).fetchone()['c']
        rows = conn.execute(
            """SELECT s.id, s.filename, s.title, s.artist, s.file_size,
                      s.duration, s.review_status, s.created_at,
                      u.username as uploader
               FROM songs s LEFT JOIN users u ON s.uploaded_by = u.id
               WHERE s.is_system = 0
               ORDER BY s.created_at DESC LIMIT ? OFFSET ?""",
            (limit, offset)
        ).fetchall()
    return {
        'total': total,
        'page': page,
        'limit': limit,
        'pages': (total + limit - 1) // limit,
        'logs': rows_to_list(rows),
    }


@router.get("/logs/download")
async def admin_download_logs(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    _: dict = Depends(require_admin),
):
    """下载日志"""
    offset = (page - 1) * limit
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) as c FROM download_history").fetchone()['c']
        rows = conn.execute(
            """SELECT dh.id, dh.downloaded_at,
                      u.username as downloader,
                      s.title, s.artist, s.filename
               FROM download_history dh
               LEFT JOIN users u ON dh.user_id = u.id
               LEFT JOIN songs s ON dh.song_id = s.id
               ORDER BY dh.downloaded_at DESC LIMIT ? OFFSET ?""",
            (limit, offset)
        ).fetchall()
    return {
        'total': total,
        'page': page,
        'limit': limit,
        'pages': (total + limit - 1) // limit,
        'logs': rows_to_list(rows),
    }


# ── 分类管理 ──────────────────────────────────────────────────────────────

@router.get("/categories")
async def admin_list_categories(_: dict = Depends(require_admin)):
    """管理员查看所有分类（系统+用户，含空分类，含创建者用户名）"""
    with db() as conn:
        rows = conn.execute(
            """SELECT c.*,
                      COALESCE(u.username, 'system') as creator_name,
                      COUNT(s.id) as real_count
               FROM categories c
               LEFT JOIN users u ON c.created_by = u.id
               LEFT JOIN songs s ON s.category_id = c.id
                 AND (s.is_system=1 OR s.review_status='approved')
               GROUP BY c.id ORDER BY c.is_system DESC, real_count DESC"""
        ).fetchall()
    return rows_to_list(rows)


@router.get("/playlists")
async def admin_list_playlists(_: dict = Depends(require_admin)):
    """管理员查看所有用户播放列表"""
    with db() as conn:
        rows = conn.execute(
            """SELECT p.*, u.username as owner_name,
                      (SELECT COUNT(*) FROM playlist_songs ps WHERE ps.playlist_id = p.id) as song_count
               FROM user_playlists p
               LEFT JOIN users u ON p.user_id = u.id
               ORDER BY p.created_at DESC"""
        ).fetchall()
    return rows_to_list(rows)


@router.put("/playlists/{playlist_id}/rename")
async def admin_rename_playlist(
    playlist_id: int,
    req: RenameCategoryRequest,
    _: dict = Depends(require_admin),
):
    """管理员重命名用户播放列表"""
    with db() as conn:
        pl = conn.execute("SELECT id FROM user_playlists WHERE id = ?", (playlist_id,)).fetchone()
        if not pl:
            raise HTTPException(404, detail="播放列表不存在")
        conn.execute("UPDATE user_playlists SET name = ? WHERE id = ?", (req.name, playlist_id))
    return {'message': f'已重命名为 {req.name}'}


@router.post("/categories", status_code=201)
async def admin_create_category(
    req: CreateCategoryRequest,
    _: dict = Depends(require_admin),
):
    """管理员新建系统分类"""
    with db() as conn:
        exists = conn.execute("SELECT id FROM categories WHERE name = ?", (req.name,)).fetchone()
        if exists:
            raise HTTPException(400, detail="分类已存在")
        cursor = conn.execute(
            "INSERT INTO categories (name, description, is_system, created_by) VALUES (?, ?, 1, 0)",
            (req.name, req.description)
        )
        return row_to_dict(conn.execute("SELECT * FROM categories WHERE id = ?", (cursor.lastrowid,)).fetchone())


@router.put("/categories/{cat_id}/rename")
async def admin_rename_category(
    cat_id: int,
    req: RenameCategoryRequest,
    _: dict = Depends(require_admin),
):
    """重命名分类"""
    with db() as conn:
        cat = conn.execute("SELECT id FROM categories WHERE id = ?", (cat_id,)).fetchone()
        if not cat:
            raise HTTPException(404, detail="分类不存在")
        dup = conn.execute("SELECT id FROM categories WHERE name = ? AND id != ?", (req.name, cat_id)).fetchone()
        if dup:
            raise HTTPException(400, detail="目标名称已存在")
        conn.execute("UPDATE categories SET name = ? WHERE id = ?", (req.name, cat_id))
    return {'message': f'已重命名为 {req.name}'}


@router.post("/categories/{cat_id}/merge")
async def admin_merge_category(
    cat_id: int,
    req: MergeCategoryRequest,
    _: dict = Depends(require_admin),
):
    """合并分类：将 cat_id 下的所有歌曲移到 target_id，然后删除 cat_id"""
    if cat_id == req.target_id:
        raise HTTPException(400, detail="不能合并到自身")
    with db() as conn:
        src = conn.execute("SELECT id, name FROM categories WHERE id = ?", (cat_id,)).fetchone()
        tgt = conn.execute("SELECT id, name FROM categories WHERE id = ?", (req.target_id,)).fetchone()
        if not src or not tgt:
            raise HTTPException(404, detail="分类不存在")
        conn.execute("UPDATE songs SET category_id = ? WHERE category_id = ?", (req.target_id, cat_id))
        conn.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
        # 更新目标分类计数
        conn.execute("""UPDATE categories SET song_count = (
            SELECT COUNT(*) FROM songs WHERE category_id = ?
              AND (is_system=1 OR review_status='approved')
        ) WHERE id = ?""", (req.target_id, req.target_id))
    logger.info(f"分类合并: {src['name']}({cat_id}) → {tgt['name']}({req.target_id})")
    return {'message': f'已合并到 {tgt["name"]}'}


@router.delete("/categories/{cat_id}")
async def admin_delete_category(
    cat_id: int,
    delete_songs: bool = Query(False, description="是否连同歌曲一起删除"),
    admin: dict = Depends(require_admin),
):
    """
    删除分类
    - delete_songs=false（默认）：仅删除空分类，有歌曲则拒绝
    - delete_songs=true：删除分类 + 分类下 admin 自己上传的歌曲（其他用户歌曲移到未分类）
    """
    import os
    from pathlib import Path
    from app.config import config

    with db() as conn:
        cat = conn.execute("SELECT id, name FROM categories WHERE id = ?", (cat_id,)).fetchone()
        if not cat:
            raise HTTPException(404, detail="分类不存在")

        song_count = conn.execute(
            "SELECT COUNT(*) as c FROM songs WHERE category_id = ?", (cat_id,)
        ).fetchone()['c']

        if not delete_songs:
            if song_count > 0:
                raise HTTPException(400, detail=f"分类下有 {song_count} 首歌曲，请选择连同删除或先移走歌曲")
            conn.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
            return {'message': f'已删除空分类 {cat["name"]}'}

        # delete_songs=true: 删 admin 的歌，其余用户的歌移到无分类
        admin_songs = conn.execute(
            "SELECT id, file_path FROM songs WHERE category_id = ? AND uploaded_by = 0",
            (cat_id,)
        ).fetchall()
        # 删除 admin 的系统歌曲文件
        upload_abs = str(Path(config.UPLOAD_PATH).resolve())
        sys_abs = str(Path(config.SYSTEM_MUSIC_PATH).resolve())
        for s in admin_songs:
            try:
                fp = str(Path(s['file_path']).resolve())
                if (fp.startswith(upload_abs) or fp.startswith(sys_abs)) and os.path.exists(fp):
                    os.remove(fp)
            except Exception:
                pass
        conn.execute("DELETE FROM songs WHERE category_id = ? AND uploaded_by = 0", (cat_id,))
        # 其他用户的歌曲：置空分类
        conn.execute("UPDATE songs SET category_id = NULL WHERE category_id = ?", (cat_id,))
        conn.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
        deleted = len(admin_songs)
    logger.info(f"分类删除: {cat['name']}({cat_id}), 删除歌曲={deleted}")
    return {'message': f'已删除分类 {cat["name"]}，删除 {deleted} 首系统歌曲'}


# ── 歌曲共享控制 ──────────────────────────────────────────────────────────

@router.put("/songs/{song_id}/share")
async def admin_toggle_share(
    song_id: int,
    req: ToggleShareRequest,
    _: dict = Depends(require_admin),
):
    """管理员控制歌曲共享状态（公开/隐藏），不删除文件"""
    with db() as conn:
        song = conn.execute("SELECT id, review_status FROM songs WHERE id = ?", (song_id,)).fetchone()
        if not song:
            raise HTTPException(404, detail="歌曲不存在")
        conn.execute("UPDATE songs SET is_shared = ? WHERE id = ?", (1 if req.shared else 0, song_id))
    action = '公开' if req.shared else '隐藏'
    logger.info(f"管理员{action}歌曲: song_id={song_id}")
    return {'message': f'歌曲已{action}'}


# ── 删除申请管理 ──────────────────────────────────────────────────────────

@router.get("/delete-requests")
async def admin_list_delete_requests(
    status: str = Query('pending'),
    _: dict = Depends(require_admin),
):
    """查看删除申请列表，含用户上传保有数和去重下载文件数"""
    with db() as conn:
        where = "WHERE dr.status = ?" if status != 'all' else ""
        params = [status] if status != 'all' else []
        rows = conn.execute(
            f"""SELECT dr.*, s.title, s.artist, s.filename, s.file_size, s.duration,
                       s.play_count, s.review_status as song_status, s.category_id,
                       c.name as category_name,
                       u.username, u.upload_count, u.download_credits
               FROM delete_requests dr
               LEFT JOIN songs s ON dr.song_id = s.id
               LEFT JOIN users u ON dr.user_id = u.id
               LEFT JOIN categories c ON s.category_id = c.id
               {where} ORDER BY dr.created_at DESC LIMIT 100""",
            params
        ).fetchall()
        result = rows_to_list(rows)

        # 为每个申请补充用户的实际保有歌曲数和去重下载文件数
        user_ids = {r['user_id'] for r in result if r.get('user_id')}
        user_stats = {}
        for uid in user_ids:
            # 当前保有上传歌曲数（数据库里实际存在的）
            owned = conn.execute(
                "SELECT COUNT(*) as c FROM songs WHERE uploaded_by = ? AND is_system = 0", (uid,)
            ).fetchone()['c']
            # 去重下载文件数（排除自己上传的，同一 song_id 只算一次）
            dl_unique = conn.execute(
                """SELECT COUNT(DISTINCT dh.song_id) as c
                   FROM download_history dh
                   LEFT JOIN songs s ON dh.song_id = s.id
                   WHERE dh.user_id = ?
                     AND (s.uploaded_by IS NULL OR s.uploaded_by != ?)""",
                (uid, uid)
            ).fetchone()['c']
            user_stats[uid] = {'songs_owned': owned, 'unique_downloads': dl_unique}

        for r in result:
            st = user_stats.get(r.get('user_id'), {})
            r['user_songs_owned'] = st.get('songs_owned', 0)
            r['user_unique_downloads'] = st.get('unique_downloads', 0)

    return result


class ReviewDeleteRequest(BaseModel):
    action: str = Field(pattern=r'^(approve|reject)$')
    notes: str = Field(default='', max_length=500)


@router.post("/delete-requests/{req_id}/review")
async def admin_review_delete_request(
    req_id: int,
    req: ReviewDeleteRequest,
    admin: dict = Depends(require_admin),
):
    """审核删除申请：approve → 执行删除；reject → 驳回"""
    with db() as conn:
        dr = conn.execute("SELECT * FROM delete_requests WHERE id = ?", (req_id,)).fetchone()
        if not dr:
            raise HTTPException(404, detail="申请不存在")
        if dr['status'] != 'pending':
            raise HTTPException(400, detail="该申请已处理")

        if req.action == 'approve':
            song = conn.execute("SELECT * FROM songs WHERE id = ?", (dr['song_id'],)).fetchone()
            if song:
                from app.music.service import _do_delete_song
                _do_delete_song(conn, row_to_dict(song))
            conn.execute(
                "UPDATE delete_requests SET status='approved', admin_notes=?, resolved_at=datetime('now') WHERE id=?",
                (req.notes, req_id)
            )
            logger.info(f"删除申请通过: req={req_id}, song={dr['song_id']}, admin={admin['username']}")
            return {'message': '已批准删除，文件已移除'}
        else:
            conn.execute(
                "UPDATE delete_requests SET status='rejected', admin_notes=?, resolved_at=datetime('now') WHERE id=?",
                (req.notes, req_id)
            )
            logger.info(f"删除申请驳回: req={req_id}, admin={admin['username']}")
            return {'message': '已驳回删除申请'}


# ── 用户歌曲管理 ──────────────────────────────────────────────────────────

@router.get("/users/{user_id}/songs")
async def admin_user_songs(
    user_id: int,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    _: dict = Depends(require_admin),
):
    """查看指定用户上传的所有歌曲"""
    offset = (page - 1) * limit
    with db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) as c FROM songs WHERE uploaded_by = ?", (user_id,)
        ).fetchone()['c']
        rows = conn.execute(
            """SELECT s.*, c.name as category_name
               FROM songs s LEFT JOIN categories c ON s.category_id = c.id
               WHERE s.uploaded_by = ?
               ORDER BY s.created_at DESC LIMIT ? OFFSET ?""",
            (user_id, limit, offset)
        ).fetchall()
    return {
        'total': total, 'page': page, 'limit': limit,
        'pages': (total + limit - 1) // limit,
        'songs': rows_to_list(rows),
    }


@router.put("/songs/{song_id}/rename")
async def admin_rename_song(
    song_id: int,
    req: RenameSongRequest,
    _: dict = Depends(require_admin),
):
    """管理员重命名歌曲（修改标题和歌手）"""
    with db() as conn:
        song = conn.execute("SELECT id FROM songs WHERE id = ?", (song_id,)).fetchone()
        if not song:
            raise HTTPException(404, detail="歌曲不存在")
        conn.execute(
            "UPDATE songs SET title = ?, artist = ? WHERE id = ?",
            (req.title, req.artist, song_id)
        )
    logger.info(f"管理员改名: song_id={song_id}, title={req.title}, artist={req.artist}")
    return {'message': f'已改名为 {req.artist} - {req.title}' if req.artist else f'已改名为 {req.title}'}


# ── 审核通过后自动 AI 分类 ────────────────────────────────────────────

async def _auto_classify_song(song_id: int, song: dict):
    """后台任务：用 AI 智能分类审核通过的歌曲（联网查询歌曲信息 → 匹配系统分类）"""
    filename = song.get('filename', '')
    title = song.get('title', '')
    artist = song.get('artist', '')
    try:
        from app.ai.classifier import classify_song
        cat_name = await classify_song(filename, title, artist)
    except Exception as e:
        logger.error(f"分类失败: song_id={song_id}, error={e}")
        from app.ai.classifier import _rule_based_classify
        cat_name = _rule_based_classify(filename)

    with db() as conn:
        row = conn.execute("SELECT id FROM categories WHERE name = ?", (cat_name,)).fetchone()
        if row:
            cat_id = row['id']
        else:
            cursor = conn.execute(
                "INSERT INTO categories (name, is_system, created_by) VALUES (?, 1, 0)", (cat_name,)
            )
            cat_id = cursor.lastrowid
        conn.execute("UPDATE songs SET category_id = ? WHERE id = ?", (cat_id, song_id))
        conn.execute("""UPDATE categories SET song_count = (
            SELECT COUNT(*) FROM songs WHERE category_id = ?
              AND (is_system=1 OR review_status='approved')
        ) WHERE id = ?""", (cat_id, cat_id))
    logger.info(f"自动分类完成: song_id={song_id}, category={cat_name}")
