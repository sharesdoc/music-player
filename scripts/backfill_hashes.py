"""
file_hash 回填脚本
对数据库中 file_hash IS NULL 的歌曲计算 SHA-256 并写入
- 文件不存在则记录 warning，跳过（不影响其他条目）
- 支持 --dry-run 只打印，不写库
- 幂等：可重复运行，只处理 hash 为空的行

用法:
    python scripts/backfill_hashes.py [--dry-run]
"""
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import init_db, db
from app.music.service import compute_file_hash

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)


def backfill(dry_run: bool = False):
    init_db()

    with db() as conn:
        rows = conn.execute(
            "SELECT id, file_path, filename FROM songs WHERE file_hash IS NULL"
        ).fetchall()

    total = len(rows)
    logger.info(f"需要回填哈希的歌曲: {total} 首 {'[DRY RUN]' if dry_run else ''}")

    updated = skipped = errors = 0

    for row in rows:
        song_id = row['id']
        file_path = row['file_path']
        filename = row['filename']

        fp = Path(file_path)
        if not fp.exists():
            logger.warning(f"文件不存在，跳过: {file_path}")
            skipped += 1
            continue

        try:
            file_bytes = fp.read_bytes()
            file_hash = compute_file_hash(file_bytes)
        except Exception as e:
            logger.error(f"读取/计算哈希失败 [{filename}]: {e}")
            errors += 1
            continue

        if dry_run:
            logger.info(f"[DRY] id={song_id} {filename} → {file_hash[:16]}…")
            updated += 1
            continue

        try:
            with db() as conn:
                # 检查是否有其他歌曲已有相同哈希（内容重复的文件）
                dup = conn.execute(
                    "SELECT id, filename, file_path FROM songs WHERE file_hash = ? AND id != ?",
                    (file_hash, song_id)
                ).fetchone()
                if dup:
                    dup_abs  = str(Path(dup['file_path']).resolve())
                    self_abs = str(fp.resolve())
                    logger.warning(
                        f"内容重复:\n"
                        f"  当前: id={song_id}  {self_abs}\n"
                        f"  已有: id={dup['id']}  {dup_abs}"
                    )
                conn.execute(
                    "UPDATE songs SET file_hash = ? WHERE id = ?",
                    (file_hash, song_id)
                )
            updated += 1
            if updated % 100 == 0:
                logger.info(f"进度: {updated}/{total}")
        except Exception as e:
            logger.error(f"写库失败 id={song_id} [{filename}]: {e}")
            errors += 1

    logger.info("=" * 50)
    logger.info(
        f"{'[DRY RUN] ' if dry_run else ''}回填完成: "
        f"更新 {updated} 首，跳过 {skipped} 首，失败 {errors} 首"
    )
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="为存量歌曲回填 file_hash")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不写库")
    args = parser.parse_args()
    backfill(dry_run=args.dry_run)
