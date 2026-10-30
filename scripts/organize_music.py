"""
音乐文件物理归类脚本
根据数据库中的分类信息，将音乐文件移动到对应的分类子目录
绝对不删除任何文件，只执行移动操作
只整理 SYSTEM_MUSIC_PATH 下的文件，不动 uploads 目录

用法:
    python scripts/organize_music.py [--dry-run] [--music-path /path]
    --dry-run  只打印计划，不实际移动
"""
import argparse
import logging
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import config
from app.database import db, rows_to_list

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def sanitize_dir_name(name: str) -> str:
    """将分类名转为合法目录名（去掉特殊字符）"""
    invalid = r'\/:*?"<>|'
    for c in invalid:
        name = name.replace(c, '_')
    return name.strip() or 'uncategorized'


def organize(music_root: str, dry_run: bool = False):
    root = Path(music_root).resolve()
    if not root.exists():
        logger.error(f"路径不存在: {root}")
        sys.exit(1)

    logger.info(f"整理目录: {root} {'[DRY RUN]' if dry_run else ''}")

    # 读取所有系统歌曲及其分类
    with db() as conn:
        rows = conn.execute(
            """SELECT s.file_path, s.filename, c.name as category_name
               FROM songs s
               LEFT JOIN categories c ON s.category_id = c.id
               WHERE s.is_system = 1""",
        ).fetchall()

    songs = rows_to_list(rows)
    moved = 0
    skipped = 0
    errors = 0

    for song in songs:
        src = Path(song['file_path']).resolve()
        cat_name = sanitize_dir_name(song['category_name'] or 'uncategorized')

        # 目标目录：music_root/分类名/
        target_dir = root / cat_name
        target_path = target_dir / src.name

        # 如果文件已在正确位置，跳过
        if src == target_path:
            skipped += 1
            continue

        # 文件不存在（可能已被移动过）
        if not src.exists():
            logger.warning(f"文件不存在，跳过: {src}")
            errors += 1
            continue

        # 目标已有同名文件，跳过（安全起见不覆盖）
        if target_path.exists():
            logger.warning(f"目标已存在，跳过: {target_path}")
            skipped += 1
            continue

        if dry_run:
            logger.info(f"[DRY] 移动: {src.name} → {cat_name}/")
            moved += 1
            continue

        # 创建目标目录
        target_dir.mkdir(parents=True, exist_ok=True)

        try:
            shutil.move(str(src), str(target_path))
            # 更新数据库中的路径
            with db() as conn:
                conn.execute(
                    "UPDATE songs SET file_path = ? WHERE file_path = ?",
                    (str(target_path), str(src))
                )
            logger.info(f"✓ {src.name} → {cat_name}/")
            moved += 1
        except Exception as e:
            logger.error(f"移动失败 {src.name}: {e}")
            errors += 1

    logger.info("=" * 50)
    logger.info(f"{'[DRY RUN] ' if dry_run else ''}整理完成: 移动 {moved} 个，跳过 {skipped} 个，失败 {errors} 个")
    if dry_run:
        logger.info("使用 --dry-run 去掉后可实际执行移动")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="音乐文件物理归类")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不实际移动")
    parser.add_argument("--music-path", default=config.SYSTEM_MUSIC_PATH)
    args = parser.parse_args()
    organize(args.music_path, dry_run=args.dry_run)
