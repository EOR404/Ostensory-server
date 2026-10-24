from __future__ import annotations

import argparse
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="备份 Ostensory 数据库、密钥和图片")
    parser.add_argument("--data-dir", default="data", help="Ostensory 数据目录")
    parser.add_argument("--output", default="backups", help="备份输出目录")
    args = parser.parse_args()

    data_dir = Path(args.data_dir).resolve()
    db_path = data_dir / "ostensory.sqlite3"
    if not db_path.exists():
        raise SystemExit(f"找不到数据库：{db_path}")
    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = output_dir / f"ostensory-{stamp}.tar.gz"

    with tempfile.TemporaryDirectory(prefix="ostensory-backup-") as temp:
        staging = Path(temp) / "ostensory"
        staging.mkdir()
        # SQLite backup API includes committed WAL content consistently.
        source = sqlite3.connect(str(db_path))
        target = sqlite3.connect(str(staging / "ostensory.sqlite3"))
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        for name in ("master.key", "secret.key"):
            path = data_dir / name
            if path.exists():
                shutil.copy2(path, staging / name)
        for name in ("originals", "thumbs", "previews", "geoip"):
            path = data_dir / name
            if path.exists():
                shutil.copytree(path, staging / name)
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(staging, arcname="ostensory")
        try:
            # The archive contains the master key; do not leave it world-readable.
            archive.chmod(0o600)
        except OSError:
            pass

    print(archive)
    print("备份包含数据库、主密钥、会话密钥和图片；请按密钥同等强度保管。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())