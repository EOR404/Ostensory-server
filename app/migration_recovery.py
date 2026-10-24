from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("ostensory.migration")
MARKER_SUFFIX = ".migration.json"
STATE_ENV = "OSTENSORY_MIGRATION_STATE_DIR"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sync_filesystem() -> None:
    sync = getattr(os, "sync", None)
    if sync is None:
        return
    try:
        sync()
    except OSError:
        pass


def fallback_state_dir(server_dir: Path) -> Path:
    """Where backups go when the data directory's parent is not writable.

    This is required by the default Docker layout: ``OSTENSORY_DATA_DIR=/data``
    is a bind mount and its parent (``/``) is not writable by the service user.
    """

    configured = os.environ.get(STATE_ENV, "").strip()
    if configured:
        return Path(configured).resolve()
    return (Path(server_dir) / "migration-backups").resolve()


def marker_path(data_dir: Path, root: Path | None = None) -> Path:
    data_dir = data_dir.resolve()
    base = Path(root).resolve() if root is not None else data_dir.parent
    return base / f".{data_dir.name}{MARKER_SUFFIX}"


def _marker_roots(data_dir: Path, server_dir: Path | None) -> list[Path]:
    roots = [data_dir.parent]
    if server_dir is not None:
        # Also inspect the historical/default fallback, not only the currently
        # configured state directory.  Otherwise changing the env var before a
        # restart would hide an interrupted import's marker and start on
        # potentially half-written data.
        candidates = [
            (Path(server_dir) / "migration-backups").resolve(),
            fallback_state_dir(server_dir),
        ]
        for fallback in candidates:
            if fallback not in roots:
                roots.append(fallback)
    return roots


def write_marker(data_dir: Path, backup: Path, root: Path | None = None) -> Path:
    """Write the recovery marker before any live data is touched.

    The marker lives outside the data directory so replacing the data
    directory cannot remove the information needed to roll back.
    """

    data_dir = data_dir.resolve()
    backup = backup.resolve()
    marker = marker_path(data_dir, root)
    marker.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "created_at": _now(),
        "data_dir": str(data_dir),
        "backup": str(backup),
    }
    temp = marker.with_name(marker.name + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
        handle.flush()
        os.fsync(handle.fileno())
    try:
        temp.chmod(0o600)
    except OSError:
        pass
    os.replace(temp, marker)
    try:
        directory_fd = os.open(str(marker.parent), os.O_RDONLY)
    except OSError:
        directory_fd = None
    if directory_fd is not None:
        try:
            os.fsync(directory_fd)
        except OSError:
            pass
        finally:
            try:
                os.close(directory_fd)
            except OSError:
                pass
    return marker


def remove_marker(data_dir: Path, root: Path | None = None) -> None:
    marker = marker_path(data_dir, root)
    marker.unlink(missing_ok=True)
    try:
        directory_fd = os.open(str(marker.parent), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    except OSError:
        pass
    finally:
        try:
            os.close(directory_fd)
        except OSError:
            pass


def _is_linkish(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction is None:
        return False
    try:
        return bool(is_junction())
    except OSError:
        return False


def _walk_error(error: OSError) -> None:
    raise error


def _clear_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for entry in path.iterdir():
        if entry.is_dir() and not _is_linkish(entry):
            shutil.rmtree(entry)
        else:
            entry.unlink(missing_ok=True)


def _copy_dir(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for entry in source.iterdir():
        target = destination / entry.name
        if _is_linkish(entry):
            raise RuntimeError(f"迁移备份包含符号链接，拒绝恢复：{entry}")
        if entry.is_dir():
            _copy_dir(entry, target)
        elif entry.is_file():
            shutil.copy2(entry, target)
        else:
            raise RuntimeError(f"迁移备份包含特殊文件，拒绝恢复：{entry}")


def _valid_backup(data_dir: Path, backup: Path, root: Path) -> bool:
    try:
        if _is_linkish(backup):
            return False
        backup = backup.resolve()
        data_dir = data_dir.resolve()
        root = root.resolve()
    except OSError:
        return False
    if backup.parent != root:
        return False
    if not backup.name.startswith(data_dir.name + ".pre-migration-"):
        return False
    if not backup.is_dir() or not (backup / "ostensory.sqlite3").is_file():
        return False
    master_path = backup / "master.key"
    secret_path = backup / "secret.key"
    if not master_path.is_file() or master_path.stat().st_size != 32:
        return False
    if not secret_path.is_file() or secret_path.stat().st_size <= 0:
        return False
    # Validate the whole tree before touching live data.  A symlink or special
    # file would otherwise make _copy_dir fail only after the old data has
    # already been removed.
    try:
        for current, dirs, names in os.walk(
            backup, followlinks=False, onerror=_walk_error
        ):
            current_path = Path(current)
            for name in dirs + names:
                candidate = current_path / name
                if _is_linkish(candidate):
                    return False
                if not candidate.is_dir() and not candidate.is_file():
                    return False
    except OSError:
        return False
    try:
        conn = sqlite3.connect(str(backup / "ostensory.sqlite3"), timeout=10)
        try:
            check = [str(row[0]) for row in conn.execute("PRAGMA quick_check").fetchall()]
            if check != ["ok"]:
                return False
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if not {
                "settings",
                "users",
                "assets",
                "albums",
                "blobs",
                "storage_backends",
            }.issubset(tables):
                return False
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False
    return True


def recover_pending_migration(
    data_dir: Path, server_dir: Path | None = None
) -> Path | None:
    """Restore the pre-import data if a previous import was interrupted.

    This runs before Config reads keys or the database.  It intentionally
    refuses to guess when a marker exists but the backup is missing: starting
    with a half-applied database would be worse than refusing to start.
    """

    data_dir = data_dir.resolve()
    for root in _marker_roots(data_dir, server_dir):
        marker = marker_path(data_dir, root)
        try:
            marker.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            # Path.exists() hides permission errors.  Starting without proof
            # that no marker exists would risk serving half-written data.
            raise RuntimeError(
                f"无法检查迁移恢复标记 {marker}：{exc}。已拒绝启动。"
            ) from exc
        result = _recover_from_marker(data_dir, marker, root)
        if result is not None:
            return result
    return None


def _recover_from_marker(data_dir: Path, marker: Path, root: Path) -> Path | None:
    try:
        payload: Any = json.loads(marker.read_text(encoding="utf-8"))
        backup = Path(str(payload["backup"]))
        declared = Path(str(payload["data_dir"]))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(
            f"迁移恢复标记损坏，拒绝在不确定数据状态时启动：{marker}。"
            "请检查数据目录和备份，确认无误后手动删除该标记。"
        ) from exc

    try:
        mismatch = declared.resolve() != data_dir.resolve()
    except OSError:
        mismatch = True
    if mismatch:
        raise RuntimeError(
            f"迁移恢复标记指向另一个数据目录：{marker}（标记={declared}，当前={data_dir}）。"
            "为避免把错误的备份覆盖到当前数据，已拒绝启动；请人工核对该标记与备份。"
        )

    if not _valid_backup(data_dir, backup, root):
        # A marker exists, so an import reached the point where it intended to
        # replace data.  Without the complete backup we cannot prove whether
        # the data directory is old, new, or half-written.  Refuse to start
        # rather than silently serving a partial database.
        raise RuntimeError(
            f"迁移中断，但找不到可用的完整备份：{backup}。"
            f"请恢复该备份，或在确认数据完整后手动删除标记 {marker}。"
        )

    env_master = os.environ.get("OSTENSORY_MASTER_KEY", "").strip()
    if env_master:
        try:
            env_bytes = bytes.fromhex(env_master)
        except ValueError as exc:
            raise RuntimeError(
                "OSTENSORY_MASTER_KEY 不是合法十六进制，无法确认恢复备份可解密。"
            ) from exc
        if env_bytes != (backup / "master.key").read_bytes():
            raise RuntimeError(
                "环境变量 OSTENSORY_MASTER_KEY 与导入前备份的主密钥不一致，"
                "恢复后图片和存储凭据将无法解密。已拒绝启动；请恢复原来的环境变量，"
                "或在确认备份完整后人工处理。"
            )

    LOGGER.warning("Recovering interrupted data migration from %s", backup)
    failed = data_dir.parent / (
        f"{data_dir.name}.failed-recovery-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    )
    if data_dir.exists():
        try:
            os.replace(data_dir, failed)
        except OSError:
            # Data directories are often bind mounts (Docker); they cannot be
            # renamed.  In that case restore the contents in place.
            _clear_dir(data_dir)
            _copy_dir(backup, data_dir)
            _sync_filesystem()
            marker.unlink(missing_ok=True)
            LOGGER.warning("Restored migration backup in place: %s", backup)
            return data_dir
    # Keep the backup after recovery.  If deleting the marker fails, the next
    # start can safely repeat the copy instead of being left without a backup.
    _copy_dir(backup, data_dir)
    _sync_filesystem()
    marker.unlink(missing_ok=True)
    LOGGER.warning("Restored data directory from migration backup: %s", backup)
    return data_dir