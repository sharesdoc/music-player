"""
AI 歌词模块
使用 Claude API + httpx 搜索并解析歌词
支持同步 LRC 格式解析
"""
import json
import logging
import re
from typing import Optional, Tuple

import httpx

from app.config import config

logger = logging.getLogger(__name__)


async def fetch_lyrics(title: str, artist: str) -> Tuple[Optional[str], Optional[str]]:
    """
    获取歌词
    策略：
    1. 先尝试从 NetEase/QQ Music API 免费接口获取 LRC
    2. 如果失败，用 Claude 根据歌曲名生成大致歌词
    返回 (plain_lyrics, lrc_synced_json)
    """
    if not title:
        return None, None

    query = f"{artist} {title}".strip()
    logger.info(f"获取歌词: {query}")

    # 先尝试免费歌词接口
    lyrics, synced = await _fetch_from_netease(title, artist)
    if lyrics:
        return lyrics, synced

    # 如果开启了 AI 且有 API Key，用 Claude 生成
    if config.AI_ENABLED and config.ANTHROPIC_API_KEY:
        return await _fetch_with_claude(title, artist)

    return None, None


async def _fetch_from_netease(title: str, artist: str) -> Tuple[Optional[str], Optional[str]]:
    """
    通过 NeteaseCloudMusicApi 兼容接口搜索歌词
    使用公开免费接口（仅作演示，生产环境需申请正规授权）
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # 搜索歌曲
            search_query = f"{artist} {title}".strip()
            resp = await client.get(
                "https://music.163.com/api/search/get",
                params={"s": search_query, "type": 1, "limit": 1},
                headers={"Referer": "https://music.163.com/"}
            )
            if resp.status_code != 200:
                return None, None

            data = resp.json()
            songs = data.get('result', {}).get('songs', [])
            if not songs:
                return None, None

            song_id = songs[0]['id']

            # 获取 LRC 歌词
            lrc_resp = await client.get(
                f"https://music.163.com/api/song/lyric",
                params={"id": song_id, "lv": 1, "kv": 1, "tv": -1},
                headers={"Referer": "https://music.163.com/"}
            )
            if lrc_resp.status_code != 200:
                return None, None

            lrc_data = lrc_resp.json()
            lrc_text = lrc_data.get('lrc', {}).get('lyric', '')
            if not lrc_text or '[00:' not in lrc_text:
                return None, None

            plain = _lrc_to_plain(lrc_text)
            synced = json.dumps(_parse_lrc(lrc_text), ensure_ascii=False)
            return plain, synced

    except Exception as e:
        logger.debug(f"网易云歌词获取失败: {e}")
        return None, None


async def _fetch_with_claude(title: str, artist: str) -> Tuple[Optional[str], Optional[str]]:
    """使用 Claude API 生成/搜索歌词"""
    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

        prompt = f"""你是一个音乐助手。请为以下歌曲提供歌词（如果你知道的话）：
歌曲名：{title}
歌手：{artist if artist else '未知'}

如果你知道这首歌的歌词，请以 LRC 格式返回（时间戳格式 [mm:ss.xx]）。
如果不知道，请返回 "UNKNOWN"。
只返回歌词内容，不要其他说明文字。"""

        message = await client.messages.create(
            model=config.AI_MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )
        result = message.content[0].text.strip()
        if result == "UNKNOWN" or not result:
            return None, None

        if '[' in result and ']' in result:
            # LRC 格式
            plain = _lrc_to_plain(result)
            synced = json.dumps(_parse_lrc(result), ensure_ascii=False)
            return plain, synced
        else:
            return result, None

    except Exception as e:
        logger.error(f"Claude 歌词获取失败: {e}")
        return None, None


def _parse_lrc(lrc_text: str) -> list:
    """
    解析 LRC 格式歌词为时间+文本数组
    返回: [{"time": 12.5, "text": "歌词行"}, ...]
    """
    pattern = re.compile(r'\[(\d+):(\d+(?:\.\d+)?)\](.*)')
    lines = []
    for line in lrc_text.split('\n'):
        m = pattern.match(line.strip())
        if m:
            minutes = int(m.group(1))
            seconds = float(m.group(2))
            text = m.group(3).strip()
            if text:  # 忽略空行
                time_secs = minutes * 60 + seconds
                lines.append({"time": round(time_secs, 2), "text": text})
    return sorted(lines, key=lambda x: x['time'])


def _lrc_to_plain(lrc_text: str) -> str:
    """将 LRC 格式转换为纯文本歌词"""
    pattern = re.compile(r'\[\d+:\d+(?:\.\d+)?\]')
    lines = []
    for line in lrc_text.split('\n'):
        # 去掉时间标签
        clean = pattern.sub('', line).strip()
        # 去掉元信息行
        if clean and not re.match(r'\[(?:ti|ar|al|by|offset):', line):
            lines.append(clean)
    return '\n'.join(lines)
