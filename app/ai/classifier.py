"""
AI 音乐分类模块
使用 OpenAI 兼容接口（支持任意 OpenAI API 兼容服务）
流程：联网查询歌曲信息 → 结合系统分类描述 → 智能归类
"""
import json
import logging
import re
from typing import List, Dict, Optional

import httpx

from app.config import config
from app.database import db, rows_to_list

logger = logging.getLogger(__name__)


def get_system_categories() -> List[dict]:
    """从数据库获取所有系统分类及描述"""
    with db() as conn:
        rows = conn.execute(
            "SELECT id, name, description FROM categories WHERE is_system = 1 ORDER BY song_count DESC"
        ).fetchall()
        return rows_to_list(rows)


async def _chat_completion(messages: list, max_tokens: int = 1000) -> Optional[str]:
    """调用 OpenAI 兼容 API"""
    if not config.AI_API_KEY:
        return None
    base = config.AI_BASE_URL.rstrip('/')
    url = f"{base}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.AI_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": config.AI_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(url, json=body, headers=headers)
            r.raise_for_status()
            data = r.json()
            return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"AI API 调用失败: {e}")
        return None


async def classify_song(filename: str, title: str = '', artist: str = '') -> str:
    """
    对单首歌曲进行智能分类
    1. 获取系统所有分类及描述
    2. 用 AI 联网查询歌曲风格信息
    3. 根据查询结果匹配最佳分类
    AI 关闭或失败时 fallback 到规则分类
    """
    if not config.AI_ENABLED or not config.AI_API_KEY:
        return _rule_based_classify(filename)

    # 获取系统分类列表
    categories = get_system_categories()
    if not categories:
        return _rule_based_classify(filename)

    cat_desc = "\n".join(
        f"- {c['name']}: {c.get('description') or '(no description)'}"
        for c in categories
    )

    song_info = f"{artist} - {title}" if artist else (title or filename)

    prompt = f"""You are a music classification expert. Classify the following song into ONE of the system categories listed below.

SONG: {song_info}
FILENAME: {filename}

AVAILABLE CATEGORIES:
{cat_desc}

INSTRUCTIONS:
1. Based on the song name and artist, determine the genre, style, era, and characteristics of this song.
2. If the artist is well-known, use your knowledge of their musical style.
3. Match the song to the single most appropriate category from the list above.
4. If no category fits well, use "大千世界" (diverse/misc).

Respond with ONLY the category name in Chinese, nothing else. Example: 经典英文"""

    result = await _chat_completion([{"role": "user", "content": prompt}])
    if result:
        # 验证返回的分类名在系统分类中
        result = result.strip().strip('"').strip("'")
        cat_names = {c['name'] for c in categories}
        if result in cat_names:
            logger.info(f"AI 分类: {song_info} → {result}")
            return result
        # 模糊匹配
        for name in cat_names:
            if name in result or result in name:
                logger.info(f"AI 分类(模糊): {song_info} → {name}")
                return name

    logger.warning(f"AI 分类失败，使用规则分类: {song_info}")
    return _rule_based_classify(filename)


async def classify_songs_batch(filenames: List[str]) -> Dict[str, str]:
    """
    批量分类歌曲（兼容旧接口）
    返回 {filename: category_name}
    """
    results = {}
    for fn in filenames:
        from app.music.service import _parse_filename
        artist, title = _parse_filename(fn)
        cat = await classify_song(fn, title, artist)
        results[fn] = cat
    return results


def _rule_based_classify(filename: str) -> str:
    """
    基于规则的分类（AI 关闭或失败时的降级方案）
    根据文件名关键词快速判断分类
    """
    name_lower = filename.lower()

    # 席琳迪翁
    if any(k in name_lower for k in ['celine dion', '席琳迪翁', 'celine']):
        return "席琳迪翁"

    # 纯音乐
    if any(k in name_lower for k in [
        'instrumental', '纯音乐', '钢琴', 'piano', 'violin', '小提琴',
        'yanni', '纯乐', '器乐', 'classical'
    ]):
        return "纯音纯乐"

    # 电子舞曲
    if any(k in name_lower for k in [
        'dj ', 'remix', 'electronic', 'dance', 'edm', 'club', 'techno',
        '电音', 'disco', 'groove'
    ]):
        return "劲歌电音"

    # 欧美乡村
    if any(k in name_lower for k in ['country', 'nashville', 'bluegrass', 'folk']):
        return "欧美乡村"

    # 华语经典
    classic_cn = [
        '邓丽君', '张国荣', '梅艳芳', '陈慧娴', '林子祥', '张学友',
        '谭咏麟', '刘德华', '徐小凤', '叶丽仪', '高胜美', '陈淑桦',
        '林忆莲', '王菲', '齐秦', '罗文'
    ]
    if any(k in filename for k in classic_cn):
        return "经典老歌"

    has_chinese = bool(re.search(r'[\u4e00-\u9fff]', filename))
    has_english = bool(re.search(r'[a-zA-Z]', filename))

    if has_chinese and not has_english:
        return "经典老歌"
    if has_english and not has_chinese:
        return "经典英文"

    return "大千世界"
