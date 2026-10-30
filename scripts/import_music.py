"""
音乐库导入脚本
将现有音乐目录（含子目录/分类）扫描并导入到数据库
- 不删除、不移动任何文件（只读模式）
- 支持 mp3, m4a, flac, ogg, wav
- 从目录名自动推断分类
- 可重复执行（跳过已存在的文件）

用法:
    python scripts/import_music.py [--music-path /path/to/music] [--ai]
"""
import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# 添加项目根目录到 PATH
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import config
from app.database import init_db, db, row_to_dict
from app.music.service import get_or_create_category, _parse_filename, _get_duration, compute_file_hash

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {'.mp3', '.m4a', '.flac', '.ogg', '.wav'}


def _get_category_from_path(file_path: Path, music_root: Path) -> str:
    """
    从文件路径推断分类名
    规则：取文件所在目录名（相对于音乐根目录的第一级目录）
    例：/music/01.席琳迪翁/xxx.mp3 → "席琳迪翁"
         /music/music/xxx.mp3     → "未分类"
    """
    try:
        rel = file_path.relative_to(music_root)
        parts = rel.parts
        if len(parts) > 1:
            dir_name = parts[0]
            # 去除数字前缀（如 "01.席琳迪翁" → "席琳迪翁"）
            clean = dir_name.split('.', 1)[-1].strip() if '.' in dir_name else dir_name
            return clean or "未分类"
    except ValueError:
        pass
    return "未分类"


def scan_music_files(music_path: str) -> list:
    """扫描目录下所有支持的音频文件"""
    root = Path(music_path)
    if not root.exists():
        logger.error(f"路径不存在: {music_path}")
        return []

    files = []
    for f in root.rglob("*"):
        if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS:
            files.append(f)

    logger.info(f"扫描完成: 找到 {len(files)} 个音频文件")
    return files


def file_already_imported(file_path: str, file_hash: str) -> bool:
    """
    双重检查：文件路径 OR 文件内容哈希 任意一项命中则认为已导入
    避免同一文件以不同路径重复导入
    """
    with db() as conn:
        row = conn.execute(
            "SELECT id FROM songs WHERE file_path = ? OR (file_hash IS NOT NULL AND file_hash = ?) LIMIT 1",
            (file_path, file_hash)
        ).fetchone()
        return row is not None


def import_file(file_path: Path, music_root: Path, category_cache: dict) -> bool:
    """
    导入单个音频文件到数据库
    - 计算文件 SHA-256 哈希，支持内容去重
    - 返回 True 表示成功，False 表示跳过（已存在）
    """
    abs_path = str(file_path.resolve())

    # 读取文件内容（用于计算哈希）
    try:
        file_bytes = file_path.read_bytes()
    except OSError as e:
        logger.error(f"读取文件失败 {file_path.name}: {e}")
        raise

    file_hash = compute_file_hash(file_bytes)

    if file_already_imported(abs_path, file_hash):
        return False  # 跳过

    filename = file_path.name
    artist, title = _parse_filename(filename)
    category_name = _get_category_from_path(file_path, music_root)

    # 获取或创建分类（使用缓存避免重复查询）
    if category_name not in category_cache:
        cat = get_or_create_category(category_name, is_system=True)
        category_cache[category_name] = cat['id']
    category_id = category_cache[category_name]

    # 获取文件大小和时长
    file_size = len(file_bytes)
    duration = _get_duration(abs_path)

    with db() as conn:
        conn.execute(
            """INSERT INTO songs
               (filename, title, artist, category_id, file_path,
                file_size, duration, is_system, is_shared, uploaded_by, file_hash, review_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, 0, ?, 'approved')""",
            (filename, title, artist, category_id, abs_path,
             file_size, duration, file_hash)
        )

    return True


async def import_with_ai_classification(files: list, music_root: Path):
    """使用 AI 批量分类后导入"""
    from app.ai.classifier import classify_songs_batch

    # 取未分类文件（在 music/ 子目录或根目录下的文件）
    unclassified = [
        f for f in files
        if len(f.relative_to(music_root).parts) <= 1 or
        f.parent.name == 'music'
    ]

    if not unclassified:
        logger.info("无需 AI 分类的文件")
        return

    logger.info(f"AI 分类 {len(unclassified)} 个文件...")
    filenames = [f.name for f in unclassified]
    classification = await classify_songs_batch(filenames)
    logger.info(f"AI 分类完成: {classification}")


def update_category_song_counts():
    """更新所有分类的 song_count"""
    with db() as conn:
        conn.execute("""
            UPDATE categories SET song_count = (
                SELECT COUNT(*) FROM songs
                WHERE songs.category_id = categories.id
            )
        """)
    logger.info("✓ 分类统计已更新")


def main():
    parser = argparse.ArgumentParser(description="导入音乐库到数据库")
    parser.add_argument(
        "--music-path",
        default=config.SYSTEM_MUSIC_PATH,
        help=f"音乐库路径（默认: {config.SYSTEM_MUSIC_PATH}）"
    )
    parser.add_argument(
        "--ai",
        action="store_true",
        help="使用 AI 对未分类文件进行分类（需要 ANTHROPIC_API_KEY）"
    )
    args = parser.parse_args()

    music_path = args.music_path
    logger.info(f"开始导入音乐库: {music_path}")

    # 初始化数据库
    init_db()

    # 扫描文件
    files = scan_music_files(music_path)
    if not files:
        logger.error("没有找到音频文件，退出")
        sys.exit(1)

    music_root = Path(music_path)
    category_cache = {}
    imported = 0
    skipped = 0
    errors = 0

    logger.info("开始导入...")
    for i, f in enumerate(files, 1):
        try:
            result = import_file(f, music_root, category_cache)
            if result:
                imported += 1
                if imported % 100 == 0:
                    logger.info(f"进度: {i}/{len(files)} (已导入 {imported})")
            else:
                skipped += 1
        except Exception as e:
            logger.error(f"导入失败 {f.name}: {e}")
            errors += 1

    # AI 分类（可选）
    if args.ai and config.AI_ENABLED:
        asyncio.run(import_with_ai_classification(files, music_root))

    # 更新统计
    update_category_song_counts()

    logger.info("=" * 50)
    logger.info(f"✓ 导入完成")
    logger.info(f"  新导入: {imported} 首")
    logger.info(f"  已跳过: {skipped} 首（已存在）")
    logger.info(f"  失败: {errors} 首")
    logger.info(f"  分类数: {len(category_cache)}")


if __name__ == "__main__":
    main()
