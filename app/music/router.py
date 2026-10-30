"""
音乐路由模块
完整的 REST API，覆盖播放、上传、下载、分类、排行、评论、播放列表、背景图等
所有公开查询均应用可见性过滤（is_system=1 OR review_status='approved'）
"""
import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import (
    APIRouter, Depends, HTTPException, UploadFile, File,
    Query, BackgroundTasks, status
)
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel, Field

from app.auth.router import get_current_user, get_current_user_optional
from app.config import config
from app.music import service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["音乐"])

# ── 请求 Schema ───────────────────────────────────────────────────────────

class CreateCategoryRequest(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    description: str = Field(default='', max_length=200)


class AddCommentRequest(BaseModel):
    content: str = Field(min_length=1, max_length=500)


class CreatePlaylistRequest(BaseModel):
    name: str = Field(min_length=1, max_length=50)


class AddToPlaylistRequest(BaseModel):
    song_id: int


# ── 分类 ──────────────────────────────────────────────────────────────────

@router.get("/categories")
async def get_categories():
    """获取所有分类"""
    return service.get_all_categories()


@router.post("/categories", status_code=201)
async def create_category(
    req: CreateCategoryRequest,
    current_user: dict = Depends(get_current_user)
):
    """用户创建自定义分类"""
    try:
        return service.create_user_category(req.name, req.description, current_user['id'])
    except ValueError as e:
        raise HTTPException(400, detail=str(e))


@router.get("/categories/{category_id}/songs")
async def category_songs(
    category_id: int,
    page: int = Query(1, ge=1),
    limit: int = Query(30, ge=1, le=100),
    sort: str = Query('play_count'),
):
    """获取指定分类下的歌曲"""
    return service.list_songs(category_id=category_id, page=page, limit=limit, sort=sort)


# ── 歌曲列表与搜索 ────────────────────────────────────────────────────────

@router.get("/songs")
async def list_songs(
    category: Optional[int] = Query(None),
    artist: Optional[str] = Query(None),
    q: Optional[str] = Query(None, description="搜索关键词"),
    page: int = Query(1, ge=1),
    limit: int = Query(30, ge=1, le=100),
    sort: str = Query('play_count', description="play_count|created_at|title"),
    current_user: Optional[dict] = Depends(get_current_user_optional),
):
    """分页查询歌曲，支持按分类/歌手/关键词过滤；未登录仅见已审核歌曲"""
    uid = current_user['id'] if current_user and current_user.get('id') else None
    return service.list_songs(
        category_id=category,
        artist=artist,
        query=q,
        page=page,
        limit=limit,
        sort=sort,
        current_user_id=uid,
    )


@router.get("/songs/{song_id}")
async def get_song(song_id: int):
    """获取歌曲详情"""
    song = service.get_song_by_id(song_id)
    if not song:
        raise HTTPException(404, detail="歌曲不存在")
    return song


# ── 播放流（HTTP Range 支持） ─────────────────────────────────────────────

@router.get("/songs/{song_id}/stream")
async def stream_song(
    song_id: int,
    background_tasks: BackgroundTasks,
    request_range: Optional[str] = None,
    current_user: Optional[dict] = Depends(get_current_user_optional),
):
    """
    流式播放音频（支持 HTTP Range 断点续传）
    所有用户（含未登录）均可播放，无需消耗配额
    """
    from fastapi import Request
    file_path = service.get_file_path_secure(song_id)
    if not file_path:
        raise HTTPException(404, detail="文件不存在")

    file_size = os.path.getsize(file_path)
    ext = Path(file_path).suffix.lower().lstrip('.')
    mime_map = {'mp3': 'audio/mpeg', 'm4a': 'audio/mp4', 'flac': 'audio/flac',
                'ogg': 'audio/ogg', 'wav': 'audio/wav'}
    mime_type = mime_map.get(ext, 'audio/mpeg')

    # 记录播放（后台任务，不阻塞响应）
    user_id = current_user['id'] if current_user else None
    background_tasks.add_task(service.record_play, song_id, user_id)

    def _iter_file(start: int, end: int):
        with open(file_path, 'rb') as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk_size = min(config.CHUNK_SIZE, remaining)
                data = f.read(chunk_size)
                if not data:
                    break
                remaining -= len(data)
                yield data

    return StreamingResponse(
        _iter_file(0, file_size - 1),
        status_code=200,
        media_type=mime_type,
        headers={
            "Accept-Ranges": "bytes",
            "Content-Length": str(file_size),
            "Cache-Control": "no-cache",
        }
    )


@router.get("/songs/{song_id}/stream-range")
async def stream_song_range(
    song_id: int,
    background_tasks: BackgroundTasks,
    range_header: Optional[str] = None,
    current_user: Optional[dict] = Depends(get_current_user_optional),
):
    """带 Range 支持的流式播放（进度条 seek）"""
    from fastapi import Request
    file_path = service.get_file_path_secure(song_id)
    if not file_path:
        raise HTTPException(404, detail="文件不存在")

    file_size = os.path.getsize(file_path)
    ext = Path(file_path).suffix.lower().lstrip('.')
    mime_map = {'mp3': 'audio/mpeg', 'm4a': 'audio/mp4', 'flac': 'audio/flac',
                'ogg': 'audio/ogg', 'wav': 'audio/wav'}
    mime_type = mime_map.get(ext, 'audio/mpeg')

    user_id = current_user['id'] if current_user else None
    background_tasks.add_task(service.record_play, song_id, user_id)

    # 解析 Range 头
    start = 0
    end = file_size - 1
    status_code = 200

    if range_header and range_header.startswith("bytes="):
        try:
            byte_range = range_header[6:]
            s, e = byte_range.split("-")
            start = int(s) if s else 0
            end = int(e) if e else file_size - 1
            end = min(end, file_size - 1)
            status_code = 206
        except (ValueError, IndexError):
            pass

    content_length = end - start + 1

    def _iter_file():
        with open(file_path, 'rb') as f:
            f.seek(start)
            remaining = content_length
            while remaining > 0:
                chunk_size = min(config.CHUNK_SIZE, remaining)
                data = f.read(chunk_size)
                if not data:
                    break
                remaining -= len(data)
                yield data

    headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(content_length),
        "Cache-Control": "no-cache",
    }
    return StreamingResponse(
        _iter_file(),
        status_code=status_code,
        media_type=mime_type,
        headers=headers
    )


# ── 下载（消耗配额） ──────────────────────────────────────────────────────

@router.get("/songs/{song_id}/download")
async def download_song(
    song_id: int,
    current_user: dict = Depends(get_current_user)
):
    """
    下载歌曲文件（消耗配额）
    - 自己上传的文件：无需配额
    - 他人的文件：消耗 1 个下载配额
    """
    can, reason = service.can_download(current_user['id'], song_id)
    if not can:
        raise HTTPException(403, detail=reason)

    file_path = service.get_file_path_secure(song_id)
    if not file_path:
        raise HTTPException(404, detail="文件不存在")

    song = service.get_song_by_id(song_id)
    # 记录下载（消耗配额）
    service.record_download(current_user['id'], song_id)

    filename = song['filename']
    return FileResponse(
        path=file_path,
        media_type='application/octet-stream',
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


# ── 上传 ──────────────────────────────────────────────────────────────────

@router.post("/songs/upload", status_code=201)
async def upload_song(
    file: UploadFile = File(...),
    category_id: Optional[int] = None,
    current_user: dict = Depends(get_current_user)
):
    """
    上传音频文件
    - 支持: mp3, m4a, flac, ogg, wav
    - 最大 50MB
    - 上传成功后增加 1 个下载配额
    """
    file_bytes = await file.read()
    try:
        song = service.save_uploaded_file(
            file.filename, file_bytes, current_user['id'], category_id
        )
        return {'message': '上传成功，获得1个下载配额', 'song': song}
    except ValueError as e:
        raise HTTPException(400, detail=str(e))


# ── 删除 ──────────────────────────────────────────────────────────────────

class DeleteSongRequest(BaseModel):
    reason: str = Field(default='', max_length=500)


@router.delete("/songs/{song_id}")
async def delete_song(
    song_id: int,
    reason: Optional[str] = Query('', description="删除理由（approved 歌曲必填）"),
    current_user: dict = Depends(get_current_user)
):
    """
    删除歌曲：
    - pending/rejected → 直接删除
    - approved → 提交删除申请（需填写理由，等管理员审核）
    """
    try:
        result = service.delete_song(song_id, current_user['id'], reason or '')
        return result
    except ValueError as e:
        raise HTTPException(400, detail=str(e))


# ── 分享 ──────────────────────────────────────────────────────────────────

@router.post("/songs/{song_id}/share")
async def share_song(
    song_id: int,
    current_user: dict = Depends(get_current_user)
):
    """将自己上传的歌曲设为公开分享"""
    try:
        service.share_song(song_id, current_user['id'])
        return {'message': '歌曲已设为公开分享'}
    except ValueError as e:
        raise HTTPException(400, detail=str(e))


# ── 播放记录 ──────────────────────────────────────────────────────────────

@router.post("/songs/{song_id}/play")
async def record_play(
    song_id: int,
    current_user: Optional[dict] = Depends(get_current_user_optional)
):
    """记录一次播放事件"""
    service.record_play(song_id, current_user['id'] if current_user else None)
    return {'ok': True}



# ── 评论 ──────────────────────────────────────────────────────────────────

@router.get("/songs/{song_id}/comments")
async def get_comments(
    song_id: int,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
):
    return service.get_comments(song_id, page, limit)


@router.post("/songs/{song_id}/comments", status_code=201)
async def add_comment(
    song_id: int,
    req: AddCommentRequest,
    current_user: dict = Depends(get_current_user)
):
    try:
        return service.add_comment(song_id, current_user['id'], req.content)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))


# ── 歌手 ──────────────────────────────────────────────────────────────────

@router.get("/artists")
async def list_artists(limit: int = Query(50, ge=1, le=200)):
    return service.list_artists(limit)


@router.get("/artists/{artist_name}/songs")
async def artist_songs(
    artist_name: str,
    page: int = Query(1, ge=1),
    limit: int = Query(30, ge=1, le=100),
):
    return service.list_songs(artist=artist_name, page=page, limit=limit, sort='play_count')


# ── 排行榜 ────────────────────────────────────────────────────────────────

@router.get("/charts/hot-songs")
async def hot_songs(limit: int = Query(20, ge=1, le=100)):
    return service.get_hot_songs(limit)


@router.get("/charts/hot-categories")
async def hot_categories(limit: int = Query(10, ge=1, le=50)):
    return service.get_hot_categories(limit)


@router.get("/charts/hot-artists")
async def hot_artists(limit: int = Query(10, ge=1, le=50)):
    return service.get_hot_artists(limit)


# ── 用户相关 ──────────────────────────────────────────────────────────────

@router.get("/user/history")
async def play_history(
    current_user: dict = Depends(get_current_user),
    limit: int = Query(50, ge=1, le=200),
):
    return service.get_user_history(current_user['id'], limit)


@router.get("/user/credits")
async def user_credits(current_user: dict = Depends(get_current_user)):
    """获取用户上传/下载配额信息"""
    return service.get_user_credits(current_user['id'])


@router.get("/user/playlists")
async def user_playlists(current_user: dict = Depends(get_current_user)):
    return service.get_user_playlists(current_user['id'])


@router.post("/user/playlists", status_code=201)
async def create_playlist(
    req: CreatePlaylistRequest,
    current_user: dict = Depends(get_current_user)
):
    try:
        return service.create_playlist(current_user['id'], req.name)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))


@router.post("/user/playlists/{playlist_id}/songs")
async def add_to_playlist(
    playlist_id: int,
    req: AddToPlaylistRequest,
    current_user: dict = Depends(get_current_user)
):
    try:
        service.add_song_to_playlist(playlist_id, req.song_id, current_user['id'])
        return {'message': '已添加到播放列表'}
    except ValueError as e:
        raise HTTPException(400, detail=str(e))


@router.get("/user/playlists/{playlist_id}/songs")
async def playlist_songs(
    playlist_id: int,
    current_user: dict = Depends(get_current_user)
):
    return service.get_playlist_songs(playlist_id)


# ── 背景图 ──────────────────────────────────────────────────────────────

class BgSettingsRequest(BaseModel):
    mode: str = Field(pattern=r'^(single|slideshow)$')
    interval: int = Field(ge=3, le=60, default=10)
    active_id: Optional[int] = None


@router.post("/backgrounds/upload", status_code=201)
async def upload_background(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user)
):
    """上传背景图片 - 最多10张，每张≤10MB"""
    file_bytes = await file.read()
    try:
        bg = service.save_background_image(file.filename, file_bytes, current_user['id'])
        return {'message': '上传成功', 'background': bg}
    except ValueError as e:
        raise HTTPException(400, detail=str(e))


@router.get("/backgrounds")
async def list_backgrounds(current_user: dict = Depends(get_current_user)):
    """获取当前用户的所有背景图和设置"""
    return service.get_user_backgrounds(current_user['id'])


@router.delete("/backgrounds/{bg_id}")
async def delete_background(bg_id: int, current_user: dict = Depends(get_current_user)):
    """删除背景图"""
    try:
        service.delete_background(bg_id, current_user['id'])
        return {'message': '已删除'}
    except ValueError as e:
        raise HTTPException(400, detail=str(e))


@router.put("/backgrounds/settings")
async def update_bg_settings(
    req: BgSettingsRequest,
    current_user: dict = Depends(get_current_user)
):
    """更新背景显示设置（单张/轮播、间隔、选中图片）"""
    service.update_bg_settings(current_user['id'], req.mode, req.interval, req.active_id)
    return {'message': '设置已更新'}


@router.get("/backgrounds/{bg_id}/image")
async def serve_background_image(
    bg_id: int,
    current_user: dict = Depends(get_current_user)
):
    """获取背景图片文件"""
    file_path = service.get_background_path(bg_id, current_user['id'])
    if not file_path:
        raise HTTPException(404, detail="背景图不存在")
    return FileResponse(file_path)
