from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from starlette.responses import PlainTextResponse

from . import __version__
from .config import Config
from .database import connect, init_db, set_setting, utc_now
from .geo import GeoResolver
from .migration_recovery import marker_path, remove_marker, write_marker
from .storage import Storage

FORMAT_NAME = "ostensory-migration"
FORMAT_VERSION = 1
CHUNK = 1024 * 1024
MB = 1024 * 1024
# 0 means disk-bound: no arbitrary application-level size/count cap. The
# administrator can impose one with the OSTENSORY_MIGRATION_MAX_* variables.
DEFAULT_MAX_ARCHIVE_BYTES = 0
DEFAULT_MAX_EXTRACTED_BYTES = 0
MAX_FILES = 0
MAX_MANIFEST_BYTES = 0
MAX_SECRET_BYTES = 1024 * 1024
EXCLUDED_DATA_NAMES = {
    "tmp",
    "cache",
    "backups",
    "update.lock",
    "migration-tmp",
    "migration-backups",
}
BACKUP_EXCLUDED_NAMES = {
    "tmp",
    "cache",
    "update.lock",
    "migration-tmp",
    "migration-backups",
}
DB_NAME = "ostensory.sqlite3"
KEY_NAMES = ("master.key", "secret.key")
MANIFEST_NAME = "manifest.json"
REMOTE_PREFIX = "remote/"

UPDATE_LAST_EXPORT = "migration_last_export_at"
UPDATE_LAST_IMPORT = "migration_last_import_at"
UPDATE_LAST_BACKUP = "migration_last_backup"
UPDATE_LAST_PACKAGE = "migration_last_package"

ADMIN_PATH_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
RESERVED_ADMIN_PATHS = {
    "api",
    "media",
    "static",
    "images",
    "albums",
    "boxes",
    "download",
    "embed",
    "view",
    "unlock",
    "search",
    "browse",
    "favicon.ico",
    "healthz",
    "about",
    "branding",
    "feed",
    "upload",
    "stats",
    "language",
    "login",
    "register",
    "logout",
    "account",
    "authors",
    "auth",
    "setup",
    "admin",
}


class MigrationError(Exception):
    pass


class MigrationBusy(MigrationError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _sync_filesystem() -> None:
    """Best-effort durability barrier before/after destructive swaps.

    Linux exposes ``os.sync``; Windows has no equivalent, so this is a no-op
    there and SQLite's own synchronous writes remain the main protection.
    """

    sync = getattr(os, "sync", None)
    if sync is None:
        return
    try:
        sync()
    except OSError:
        pass


def max_archive_bytes() -> int:
    raw = os.environ.get("OSTENSORY_MIGRATION_MAX_MB", "").strip()
    if not raw:
        return DEFAULT_MAX_ARCHIVE_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_ARCHIVE_BYTES
    return max(0, value) * MB


def max_extracted_bytes() -> int:
    raw = os.environ.get("OSTENSORY_MIGRATION_MAX_EXTRACTED_MB", "").strip()
    if not raw:
        return DEFAULT_MAX_EXTRACTED_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_EXTRACTED_BYTES
    return max(0, value) * MB


def max_archive_mb() -> int:
    return max(0, max_archive_bytes() // MB)


def max_files() -> int:
    raw = os.environ.get("OSTENSORY_MIGRATION_MAX_FILES", "").strip()
    if not raw:
        return MAX_FILES
    try:
        return max(0, int(raw))
    except ValueError:
        return MAX_FILES


def max_manifest_bytes() -> int:
    raw = os.environ.get("OSTENSORY_MIGRATION_MAX_MANIFEST_MB", "").strip()
    if not raw:
        return MAX_MANIFEST_BYTES
    try:
        return max(0, int(raw)) * MB
    except ValueError:
        return MAX_MANIFEST_BYTES


def _remove_stale_entries(directory: Path, pattern: str, max_age: float = 86400) -> None:
    try:
        cutoff = time.time() - max_age
        for entry in directory.glob(pattern):
            try:
                if max_age <= 0 or entry.stat().st_mtime < cutoff:
                    if entry.is_dir() and not _is_linkish(entry):
                        shutil.rmtree(entry, ignore_errors=True)
                    else:
                        entry.unlink(missing_ok=True)
            except OSError:
                continue
    except OSError:
        return


def cleanup_stale_migration_temp(config: Config, max_age: float = 86400) -> None:
    """Best-effort cleanup of abandoned staging data, including key material."""

    _remove_stale_entries(
        config.data_dir / "tmp", "ostensory-migration-*.tar.gz", max_age=max_age
    )
    bases = [config.server_dir]
    configured = os.environ.get("OSTENSORY_MIGRATION_STATE_DIR", "").strip()
    if configured:
        bases.append(Path(configured).resolve())
    seen: set[Path] = set()
    for base in bases:
        try:
            staging = (base / "migration-tmp").resolve()
        except OSError:
            continue
        if staging in seen or _inside(config.data_dir, staging):
            continue
        seen.add(staging)
        if staging.is_dir():
            _remove_stale_entries(staging, ".ostensory-migration-*", max_age=max_age)
            _remove_stale_entries(
                staging, "ostensory-migration-*.tar.gz", max_age=max_age
            )

    # FastAPI spools multipart uploads with tempfile's ``tmp*`` names.  The
    # Docker image points TMPDIR at <state>/tmp so a crashed upload cannot fill
    # the container layer.  Clean only that dedicated directory, never /tmp.
    if configured:
        try:
            state_root = Path(configured).resolve()
            upload_tmp = Path(os.environ.get("TMPDIR", "").strip()).resolve()
        except OSError:
            upload_tmp = None
        if upload_tmp is not None and upload_tmp == state_root / "tmp":
            if upload_tmp.is_dir():
                _remove_stale_entries(upload_tmp, "tmp*", max_age=max_age)


def staging_dir(config: Config) -> Path:
    """Return a writable directory for the (potentially large) staging copy.

    Prefer the configured migration volume, because Docker's ``/data`` parent
    is read-only and the application layer may be small.  A crashed import can
    leave key material here, so old staging entries are removed opportunistically.
    """

    configured = os.environ.get("OSTENSORY_MIGRATION_STATE_DIR", "").strip()
    base = Path(configured).resolve() if configured else config.server_dir
    if _inside(config.data_dir, base):
        base = config.server_dir
    staging = base / "migration-tmp"
    staging.mkdir(parents=True, exist_ok=True)
    cleanup_stale_migration_temp(config)
    return staging


class MigrationGate:
    """Keep HTTP requests and background jobs out while data is swapped."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._active: set[int] = set()
        self._next = 0
        self._busy = False
        self._reason = ""
        self._owner: int | None = None

    def enter(self) -> int | None:
        with self._condition:
            if self._busy:
                return None
            self._next += 1
            token = self._next
            self._active.add(token)
            return token

    def leave(self, token: int | None) -> None:
        if token is None:
            return
        with self._condition:
            self._active.discard(token)
            self._condition.notify_all()

    def begin(self, current_token: int | None, timeout: float = 120.0) -> None:
        deadline = time.monotonic() + max(5.0, timeout)
        with self._condition:
            if self._busy:
                raise MigrationBusy("另一个迁移任务正在进行，请稍后再试。")
            self._busy = True
            self._reason = "正在进行数据迁移"
            try:
                while self._active - ({current_token} if current_token is not None else set()):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise MigrationBusy(
                            "还有请求正在处理，暂时无法进入维护模式；请稍后重试。"
                        )
                    self._condition.wait(remaining)
                self._owner = current_token
            except BaseException:
                # Cancellation while draining must not leave the gate stuck
                # busy; otherwise a surviving process could never migrate again.
                self._busy = False
                self._reason = ""
                self._owner = None
                self._condition.notify_all()
                raise

    def end(self) -> None:
        with self._condition:
            self._busy = False
            self._reason = ""
            self._owner = None
            self._condition.notify_all()

    def end_if_owner(self, token: int | None) -> bool:
        """Release the gate only if this request still owns it.

        A request can be interrupted between ``begin()`` returning and the
        route setting its own ``began`` flag.  Releasing by owner token closes
        that window without letting a request that merely lost the race end
        somebody else's migration.
        """

        with self._condition:
            if self._owner != token:
                return False
            self._busy = False
            self._reason = ""
            self._owner = None
            self._condition.notify_all()
            return True

    @property
    def busy(self) -> bool:
        with self._condition:
            return self._busy

    @property
    def reason(self) -> str:
        with self._condition:
            return self._reason


class MigrationMaintenanceMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        state = scope["app"].state
        gate: MigrationGate | None = getattr(state, "migration_gate", None)
        if gate is None:
            await self.app(scope, receive, send)
            return
        if getattr(state, "migration_fatal", False):
            # A failed rollback is not a maintenance pause: the live directory
            # may be ambiguous, so even the liveness probe must fail and let an
            # orchestrator restart into startup recovery.  Preserve the marker
            # and backup until that restart has proved the data.
            response = PlainTextResponse(
                "迁移回滚失败，服务已停止对外提供数据。请检查日志并重启服务，"
                "启动时会根据恢复标记还原导入前备份。",
                status_code=503,
                headers={"Retry-After": "30", "Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return
        if getattr(state, "migration_rolling_back", False):
            if str(scope.get("path", "")) == "/healthz":
                await self.app(scope, receive, send)
                return
            response = PlainTextResponse(
                "正在从导入前备份恢复数据，请稍后刷新。",
                status_code=503,
                headers={"Retry-After": "15", "Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return
        # Liveness must stay green while a large migration keeps maintenance
        # mode active; otherwise Docker/Kubernetes may consider the service dead
        # and restart it in the middle of a data swap.  This endpoint does not
        # touch the database or storage.
        if str(scope.get("path", "")) == "/healthz":
            await self.app(scope, receive, send)
            return
        if (
            scope.get("method") == "POST"
            and str(scope.get("path", "")).endswith("/migration/import")
        ):
            try:
                declared = int(
                    dict(scope.get("headers") or []).get(b"content-length", b"0") or 0
                )
            except (TypeError, ValueError):
                declared = 0
            # Leave room for multipart boundaries and form fields.  Chunked
            # uploads have no Content-Length and are capped while reading.
            limit = max_archive_bytes()
            if limit > 0 and declared > limit + 2 * MB:
                response = PlainTextResponse(
                    "上传的迁移包超过大小限制。",
                    status_code=413,
                    headers={"Cache-Control": "no-store"},
                )
                await response(scope, receive, send)
                return
        token = gate.enter()
        if token is None:
            response = PlainTextResponse(
                "数据迁移进行中，站点暂时不可用。请稍后刷新。",
                status_code=503,
                headers={"Retry-After": "15", "Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return
        scope.setdefault("state", {})["migration_token"] = token
        try:
            await self.app(scope, receive, send)
        finally:
            gate.leave(token)


def _validate_admin_path(value: str) -> str:
    cleaned = (value or "admin").strip().strip("/")
    if not ADMIN_PATH_RE.match(cleaned):
        raise MigrationError("迁移包内的后台路径格式不安全，已停止导入。")
    if cleaned.lower() in RESERVED_ADMIN_PATHS and cleaned.lower() != "admin":
        raise MigrationError("迁移包内的后台路径与系统保留路径冲突，已停止导入。")
    return cleaned


_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_WINDOWS_BAD_CHARS = set('<>:"|?*')


def _check_portable_component(part: str, *, label: str, value: str) -> None:
    """Reject names that would be reinterpreted by Windows/NTFS.

    Migration packages may be produced on Linux and imported on Windows.  A
    colon creates an alternate data stream, and names such as CON/NUL address
    devices; neither is acceptable for an archive import.
    """

    if (
        not part
        or part in {".", ".."}
        or any(ord(char) < 32 for char in part)
        or any(char in _WINDOWS_BAD_CHARS for char in part)
        or part.endswith((" ", "."))
        or part.split(".", 1)[0].upper() in _WINDOWS_RESERVED
    ):
        raise MigrationError(f"{label}不安全：{value}")


def _safe_rel(
    value: str, *, label: str = "文件路径", allow_manifest: bool = False
) -> str:
    raw = str(value or "").replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    if "\x00" in raw or re.match(r"^[A-Za-z]:", raw):
        raise MigrationError(f"{label}不安全：{value}")
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or not path.parts:
        raise MigrationError(f"{label}不安全：{value}")
    for part in path.parts:
        _check_portable_component(part, label=label, value=value)
    if not allow_manifest and path.parts[0] in {MANIFEST_NAME}:
        raise MigrationError(f"{label}不能指向清单文件：{value}")
    return path.as_posix()


def _inside(base: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(base.resolve())
    except (ValueError, OSError):
        return False
    return True


def _is_excluded(name: str, *, for_backup: bool = False) -> bool:
    excluded = BACKUP_EXCLUDED_NAMES if for_backup else EXCLUDED_DATA_NAMES
    if name in excluded:
        return True
    if name == DB_NAME or name.startswith(DB_NAME + "-") or name.startswith(DB_NAME + "."):
        return True
    if name in KEY_NAMES:
        return True
    return False


def _validate_blob_key(value: str, *, label: str = "图片存储路径") -> str:
    relative = _safe_rel(value, label=label)
    first = relative.split("/", 1)[0]
    if _is_excluded(first):
        raise MigrationError(f"{label}不能指向数据库、密钥、缓存或备份：{value}")
    return relative


def _is_linkish(path: Path) -> bool:
    """Symlinks and Windows junctions can escape the data directory."""

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
    raise MigrationError(f"无法读取数据目录：{error}")


def _iter_files(root: Path, *, skip_top: set[str] | None = None) -> Iterator[tuple[Path, str]]:
    root = root.resolve()
    skip = skip_top or set()
    if not root.exists():
        return
    for current, dirs, names in os.walk(
        root, followlinks=False, onerror=_walk_error
    ):
        current_path = Path(current)
        kept_dirs: list[str] = []
        for dir_name in sorted(dirs):
            directory = current_path / dir_name
            if current_path == root and dir_name in skip:
                continue
            if _is_linkish(directory):
                raise MigrationError(f"数据目录包含符号链接，已停止：{directory}")
            kept_dirs.append(dir_name)
        dirs[:] = kept_dirs
        for name in sorted(names):
            path = current_path / name
            if _is_linkish(path):
                raise MigrationError(f"数据目录包含符号链接，已停止：{path}")
            if not path.is_file():
                raise MigrationError(f"数据目录包含不支持的特殊文件：{path}")
            rel = _safe_rel(
                path.relative_to(root).as_posix(),
                label="数据文件路径",
            )
            if current_path == root and name in skip:
                continue
            yield path, rel


def data_usage_bytes(data_dir: Path) -> int:
    total = 0
    try:
        for path, _rel in _iter_files(data_dir, skip_top=EXCLUDED_DATA_NAMES | {DB_NAME, *KEY_NAMES}):
            try:
                total += path.stat().st_size
            except OSError:
                continue
    except MigrationError:
        return 0
    return total


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def _copy_with_hash(
    source: Path,
    target: Path,
    relative: str,
    files: dict[str, dict[str, Any]],
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if not target.is_file():
            raise MigrationError(f"迁移包目标路径冲突：{relative}")
        existing_digest, existing_size = _hash_file(target)
        if existing_size != source.stat().st_size:
            raise MigrationError(f"迁移包目标路径被不同文件占用：{relative}")
        source_digest = hashlib.sha256()
        source_size = 0
        with source.open("rb") as handle:
            while True:
                chunk = handle.read(CHUNK)
                if not chunk:
                    break
                source_size += len(chunk)
                source_digest.update(chunk)
        if source_size != existing_size or source_digest.hexdigest() != existing_digest:
            raise MigrationError(f"迁移包目标路径被不同文件占用：{relative}")
        files[relative] = {"size": existing_size, "sha256": existing_digest}
        return
    temp = target.with_name(target.name + f".{uuid.uuid4().hex}.part")
    digest = hashlib.sha256()
    size = 0
    try:
        with source.open("rb") as src, temp.open("wb") as dst:
            while True:
                chunk = src.read(CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
                dst.write(chunk)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    files[relative] = {"size": size, "sha256": digest.hexdigest()}


def _record_existing_file(path: Path, relative: str, files: dict[str, dict[str, Any]]) -> None:
    digest, size = _hash_file(path)
    files[relative] = {"size": size, "sha256": digest}


def _write_key(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _sqlite_integrity(path: Path) -> None:
    conn = sqlite3.connect(str(path), timeout=30)
    try:
        rows = [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()]
        if rows != ["ok"]:
            raise MigrationError("数据库完整性检查失败：" + "; ".join(rows[:4]))
        foreign = conn.execute("PRAGMA foreign_key_check").fetchmany(5)
        if foreign:
            raise MigrationError("数据库外键检查失败，迁移包可能损坏。")
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        required = {"settings", "users", "assets", "albums", "blobs", "storage_backends"}
        missing = required - tables
        if missing:
            raise MigrationError("不像是 Ostensory 数据包，缺少表：" + ", ".join(sorted(missing)))
    except sqlite3.DatabaseError as exc:
        raise MigrationError(f"数据库无法读取：{exc}") from exc
    finally:
        conn.close()


def _database_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in ("users", "assets", "albums", "boxes", "blobs", "api_tokens"):
        try:
            counts[table] = int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
        except sqlite3.Error:
            counts[table] = 0
    return counts


def _data_disk_free(data_dir: Path) -> int:
    try:
        return shutil.disk_usage(data_dir).free
    except OSError:
        return 0


def _require_disk_space(data_dir: Path, needed: int, label: str) -> None:
    free = _data_disk_free(data_dir)
    if free and free < needed:
        raise MigrationError(
            f"磁盘剩余空间不足，无法{label}。至少还需要 "
            f"{max(0, needed - free) / MB:.1f} MB。"
        )


@dataclass
class ExportResult:
    path: Path
    size: int
    files: int
    counts: dict[str, int]
    remote_localized: int
    total_bytes: int


@dataclass
class ImportPlan:
    root: Path
    data_dir: Path
    manifest: dict[str, Any]
    counts: dict[str, int]
    files: dict[str, dict[str, Any]]
    total_bytes: int
    master_key: bytes
    secret_key: bytes
    remote_localized: int = 0

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


@dataclass
class ImportResult:
    backup_path: Path
    admin_path: str
    counts: dict[str, int]
    files: int
    remote_localized: int


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _copy_export_data(
    source: Path,
    target: Path,
    files: dict[str, dict[str, Any]],
) -> None:
    for entry in sorted(source.iterdir(), key=lambda item: item.name):
        name = entry.name
        if _is_excluded(name):
            continue
        _check_portable_component(name, label="数据文件路径", value=name)
        # Validate the packaged relative name, not just the filesystem name.
        # A top-level directory literally called ``manifest.json`` would
        # otherwise produce entries that the importer rejects.
        safe_name = _safe_rel(name, label="数据文件路径")
        if _is_linkish(entry):
            raise MigrationError(f"数据目录包含符号链接，已停止：{entry}")
        if entry.is_dir():
            for path, rel in _iter_files(entry):
                _copy_with_hash(
                    path, target / safe_name / rel, f"{safe_name}/{rel}", files
                )
        elif entry.is_file():
            _copy_with_hash(entry, target / safe_name, safe_name, files)
        else:
            raise MigrationError(f"数据目录包含不支持的特殊文件，已停止：{entry}")


def _localize_remote_blobs(
    config: Config,
    storage: Storage,
    conn: sqlite3.Connection,
    data_root: Path,
    files: dict[str, dict[str, Any]],
) -> int:
    """Download remote objects and rewrite their blob rows to local paths.

    The migration package must be usable when the old remote storage is gone.
    Existing blobs are therefore copied into ``migrated/<backend>/...`` and the
    database points at that local path.  The backend configuration rows remain
    available for future uploads.
    """

    rows = conn.execute("SELECT * FROM blobs ORDER BY id").fetchall()
    localized = 0
    for row in rows:
        updates: dict[str, str] = {}
        for field_name in ("storage_key", "thumb_key", "preview_key"):
            key = str(row[field_name] or "")
            if not key:
                if field_name == "storage_key":
                    raise MigrationError(f"图片 blob {row['id']} 缺少主文件路径，无法打包。")
                updates[field_name] = ""
                continue
            backend_id, inner = storage._split_key(key)
            if backend_id is None:
                local_parts = PurePosixPath(str(key).replace("\\", "/")).parts
                if local_parts and local_parts[0] in EXCLUDED_DATA_NAMES:
                    raise MigrationError(
                        f"图片引用了临时/缓存路径，无法打包：{key}"
                    )
                if str(key).replace("\\", "/").startswith(REMOTE_PREFIX):
                    raise MigrationError(
                        f"本地图片路径不能使用 remote/ 前缀，无法打包：{key}"
                    )
                try:
                    relative = _validate_blob_key(key, label="本地图片路径")
                except MigrationError as exc:
                    raise MigrationError(
                        f"图片路径无法安全打包：{row['sha256']} ({key})"
                    ) from exc
                try:
                    local_path = storage.path_for(key)
                except Exception as exc:
                    raise MigrationError(
                        f"图片文件无法读取：{row['sha256']} ({key})：{exc}"
                    ) from exc
                if not local_path or not local_path.is_file():
                    raise MigrationError(
                        f"图片文件缺失：{row['sha256']} ({key})，已停止导出。"
                    )
                if local_path.stat().st_size == 0:
                    raise MigrationError(
                        f"图片文件为空：{row['sha256']} ({key})，已停止导出。"
                    )
                packaged = data_root / relative
                if not packaged.is_file():
                    raise MigrationError(
                        f"图片文件没有包含在迁移包中：{row['sha256']} ({key})，已停止导出。"
                    )
                source_digest, source_size = _hash_file(local_path)
                packaged_digest, packaged_size = _hash_file(packaged)
                if (source_size, source_digest) != (packaged_size, packaged_digest):
                    raise MigrationError(
                        f"图片文件在打包期间发生变化：{row['sha256']} ({key})，"
                        "请重新导出。"
                    )
                updates[field_name] = relative
                continue
            try:
                source_path = storage.path_for(key)
            except Exception as exc:
                raise MigrationError(
                    f"远程存储中的图片无法读取：{row['sha256']} ({key})：{exc}"
                ) from exc
            if not source_path or not source_path.is_file():
                raise MigrationError(
                    f"远程存储中的图片无法读取：{row['sha256']} ({key})，"
                    "请先恢复该存储方式后重试。"
                )
            if source_path.stat().st_size == 0:
                raise MigrationError(
                    f"远程存储中的图片为空：{row['sha256']} ({key})，已停止导出。"
                )
            safe_inner = _safe_rel(inner, label="远程存储路径", allow_manifest=True)
            relative = f"migrated/{backend_id}/{safe_inner}"
            target = data_root / relative
            if target.exists():
                # A user file may already occupy this path.  Never let the
                # database point at bytes that are not the remote object.
                if not target.is_file():
                    raise MigrationError(f"图片本地化路径冲突：{relative}")
                source_digest, source_size = _hash_file(source_path)
                existing_digest, existing_size = _hash_file(target)
                if (source_size, source_digest) != (existing_size, existing_digest):
                    raise MigrationError(
                        f"图片本地化路径已被其他文件占用：{relative}"
                    )
                files.setdefault(
                    relative,
                    {"size": existing_size, "sha256": existing_digest},
                )
            else:
                _copy_with_hash(source_path, target, relative, files)
            updates[field_name] = relative
            localized += 1
        conn.execute(
            "UPDATE blobs SET storage_key = ?, thumb_key = ?, preview_key = ?, backend_id = 1 "
            "WHERE id = ?",
            (
                updates.get("storage_key", ""),
                updates.get("thumb_key", ""),
                updates.get("preview_key", ""),
                int(row["id"]),
            ),
        )
    return localized


def create_export_archive(
    config: Config,
    db: sqlite3.Connection,
    storage: Storage,
) -> ExportResult:
    """Create a self-contained migration package.

    The caller should hold the migration gate.  The live database is never
    modified; all rewrites happen in the temporary snapshot.
    """

    root = Path(tempfile.mkdtemp(prefix=".ostensory-migration-export-", dir=staging_dir(config)))
    stage = root / "stage"
    data_root = stage / "data"
    data_root.mkdir(parents=True)
    archive: Path | None = None
    files: dict[str, dict[str, Any]] = {}
    try:
        db.commit()
        snapshot_path = data_root / DB_NAME
        target = sqlite3.connect(str(snapshot_path))
        try:
            db.backup(target)
        finally:
            target.close()
        snapshot = sqlite3.connect(str(snapshot_path))
        snapshot.row_factory = sqlite3.Row
        try:
            snapshot.execute("PRAGMA foreign_keys = ON")
            # Sessions are machine/browser state, not content.  Leaving them
            # out avoids carrying usable login cookies into a downloadable
            # archive; import also purges the table for older packages.
            snapshot.execute("DELETE FROM sessions")
            snapshot.execute("DELETE FROM login_throttle")
            counts = _database_counts(snapshot)
            if len(config.secret_key) > MAX_SECRET_BYTES:
                raise MigrationError("当前会话密钥过长，无法安全写入迁移包。")
            _write_key(data_root / "master.key", config.master_key)
            _write_key(data_root / "secret.key", config.secret_key)
            _copy_export_data(config.data_dir, data_root, files)
            remote_localized = _localize_remote_blobs(
                config, storage, snapshot, data_root, files
            )
            set_setting(snapshot, "migration_exported_at", utc_now())
            set_setting(snapshot, "migration_export_version", __version__)
            set_setting(snapshot, "migration_localized_remote", "1" if remote_localized else "0")
            snapshot.commit()
            # A backup copied from a WAL database can inherit WAL flags.  Put
            # the standalone snapshot back into rollback-journal mode before
            # closing it so committed pages are checkpointed into the main file
            # and no -wal/-shm files can be packaged.
            snapshot.execute("PRAGMA journal_mode=DELETE")
        finally:
            snapshot.close()
        # Never let a SQLite sidecar from the snapshot connection leak into the
        # package: the importer intentionally rejects database sidecars.
        for suffix in ("-wal", "-shm", "-journal"):
            (snapshot_path.parent / f"{DB_NAME}{suffix}").unlink(missing_ok=True)
        _sqlite_integrity(snapshot_path)
        # integrity_check opens the snapshot again and can create sidecars on a
        # WAL-mode database.  Remove them before hashing/tarring so the
        # package can never contain files the importer deliberately rejects.
        for suffix in ("-wal", "-shm", "-journal"):
            try:
                (snapshot_path.parent / f"{DB_NAME}{suffix}").unlink(missing_ok=True)
            except OSError as exc:
                raise MigrationError(f"无法清理数据库临时文件：{exc}") from exc

        _record_existing_file(snapshot_path, DB_NAME, files)
        for name in KEY_NAMES:
            _record_existing_file(data_root / name, name, files)

        total_bytes = sum(int(item["size"]) for item in files.values())
        manifest = {
            "format": FORMAT_NAME,
            "format_version": FORMAT_VERSION,
            "created_at": _now(),
            "app_version": __version__,
            "database": DB_NAME,
            "data_dir": "data",
            "counts": counts,
            "files": files,
            "total_bytes": total_bytes,
            "remote_localized": remote_localized,
        }
        (stage / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        tmp_dir = staging_dir(config)
        free = _data_disk_free(tmp_dir)
        if free and free < total_bytes + 64 * MB:
            raise MigrationError(
                "磁盘剩余空间不足以生成迁移包。请先清理空间后重试。"
            )
        tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, archive_name = tempfile.mkstemp(
            prefix="ostensory-migration-", suffix=".tar.gz", dir=tmp_dir
        )
        os.close(fd)
        archive = Path(archive_name)
        with tarfile.open(archive, "w:gz", compresslevel=6) as tar:
            tar.add(stage / MANIFEST_NAME, arcname=MANIFEST_NAME)
            tar.add(data_root, arcname="data")
        try:
            archive.chmod(0o600)
        except OSError:
            pass
        return ExportResult(
            path=archive,
            size=archive.stat().st_size,
            files=len(files),
            counts=counts,
            remote_localized=remote_localized,
            total_bytes=total_bytes,
        )
    except BaseException:
        if archive is not None:
            try:
                archive.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def _extract_archive(archive: Path, root: Path) -> None:
    total = 0
    count = 0
    seen: set[str] = set()
    extracted = root / "extracted"
    extracted.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar:
                count += 1
                file_limit = max_files()
                if file_limit and count > file_limit:
                    raise MigrationError("迁移包文件数量过多，已停止。")
                name = member.name.replace("\\", "/")
                while name.startswith("./"):
                    name = name[2:]
                if not name or name.startswith("/") or "\x00" in name or re.match(r"^[A-Za-z]:", name):
                    raise MigrationError("迁移包包含不安全的绝对路径。")
                pure = PurePosixPath(name)
                for part in pure.parts:
                    _check_portable_component(
                        part, label="迁移包路径", value=name
                    )
                if pure.parts[0] not in {MANIFEST_NAME, "data"}:
                    raise MigrationError(f"迁移包含未知顶层内容：{name}")
                if len(pure.parts) > 1 and (
                    pure.parts[1].startswith(DB_NAME + "-")
                    or pure.parts[1].startswith(DB_NAME + ".")
                ):
                    raise MigrationError(
                        "迁移包包含数据库日志、旧副本或同名目录，请重新导出。"
                    )
                if name in seen:
                    raise MigrationError(f"迁移包含重复路径：{name}")
                seen.add(name)
                target = extracted.joinpath(*pure.parts)
                try:
                    target.resolve().relative_to(extracted.resolve())
                except (ValueError, OSError):
                    raise MigrationError(f"迁移包路径越界：{name}")
                if member.issym() or member.islnk():
                    raise MigrationError("迁移包包含符号链接，已停止。")
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise MigrationError(f"迁移包包含不支持的文件类型：{name}")
                if target.exists():
                    raise MigrationError(f"迁移包含重复文件：{name}")
                total += int(member.size)
                extracted_limit = max_extracted_bytes()
                if extracted_limit and total > extracted_limit:
                    raise MigrationError("迁移包解压后超过大小限制，已停止。")
                source = tar.extractfile(member)
                if source is None:
                    raise MigrationError(f"迁移包内容损坏：{name}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with source, target.open("wb") as handle:
                    shutil.copyfileobj(source, handle)
    except tarfile.TarError as exc:
        raise MigrationError(f"迁移包无法读取：{exc}") from exc
    if not (extracted / MANIFEST_NAME).is_file():
        raise MigrationError("迁移包缺少 manifest.json。")
    if not (extracted / "data").is_dir():
        raise MigrationError("迁移包缺少 data 目录。")


def _validate_manifest(
    root: Path,
    manifest_path: Path,
    data_root: Path,
    config: Config,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], int]:
    try:
        manifest_limit = max_manifest_bytes()
        if manifest_limit and manifest_path.stat().st_size > manifest_limit:
            raise MigrationError("迁移清单过大，已停止导入。")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MigrationError(f"无法读取迁移清单：{exc}") from exc
    if not isinstance(manifest, dict):
        raise MigrationError("迁移清单格式不正确。")
    if manifest.get("format") != FORMAT_NAME:
        raise MigrationError("不是 Ostensory 数据迁移包。")
    try:
        version = int(manifest.get("format_version"))
    except (TypeError, ValueError) as exc:
        raise MigrationError("迁移包格式版本不正确。") from exc
    if version != FORMAT_VERSION:
        raise MigrationError(
            f"迁移包格式版本 {version} 与当前版本 {FORMAT_VERSION} 不兼容。"
        )
    package_version = str(manifest.get("app_version") or "")
    current_match = re.match(r"^(\d+)\.(\d+)\.(\d+)", __version__)
    package_match = re.match(r"^(\d+)\.(\d+)\.(\d+)", package_version)
    if current_match and package_match:
        if tuple(int(part) for part in package_match.groups()) > tuple(
            int(part) for part in current_match.groups()
        ):
            raise MigrationError(
                f"迁移包来自更新版本 {package_version}，当前程序是 {__version__}。"
                "请先把目标服务器升级到相同或更新版本，再导入。"
            )
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise MigrationError("迁移清单缺少文件校验信息。")
    checked: dict[str, dict[str, Any]] = {}
    for key, value in files.items():
        relative = _safe_rel(str(key), label="迁移清单路径")
        if not isinstance(value, dict):
            raise MigrationError(f"迁移清单文件信息损坏：{key}")
        try:
            size = int(value.get("size"))
            digest = str(value.get("sha256") or "").lower()
        except (TypeError, ValueError) as exc:
            raise MigrationError(f"迁移清单文件信息损坏：{key}") from exc
        if size < 0 or len(digest) != 64:
            raise MigrationError(f"迁移清单文件校验值无效：{key}")
        if relative in checked:
            raise MigrationError(f"迁移清单包含重复路径：{key}")
        checked[relative] = {"size": size, "sha256": digest}
    actual = {
        rel: path
        for path, rel in _iter_files(data_root)
    }
    for relative in actual:
        if relative.startswith(DB_NAME + "-") or relative.startswith(DB_NAME + "."):
            raise MigrationError(
                "迁移包包含数据库日志或旧数据库副本，请使用当前版本重新导出。"
            )
    if set(actual) != set(checked):
        missing = sorted(set(checked) - set(actual))[:5]
        extra = sorted(set(actual) - set(checked))[:5]
        detail = []
        if missing:
            detail.append("缺少 " + ", ".join(missing))
        if extra:
            detail.append("多出 " + ", ".join(extra))
        raise MigrationError("迁移包文件清单不一致：" + "；".join(detail))
    total_bytes = 0
    for relative, path in actual.items():
        digest, size = _hash_file(path)
        expected = checked[relative]
        if size != int(expected["size"]) or digest != expected["sha256"]:
            raise MigrationError(f"迁移包文件校验失败：{relative}")
        total_bytes += size

    expected_db = str(manifest.get("database") or DB_NAME)
    if expected_db != DB_NAME:
        raise MigrationError("迁移包数据库名称不正确。")
    if not (data_root / DB_NAME).is_file():
        raise MigrationError("迁移包缺少数据库。")
    master_path = data_root / "master.key"
    secret_path = data_root / "secret.key"
    if not master_path.is_file() or master_path.stat().st_size != 32:
        raise MigrationError("迁移包主密钥文件损坏：master.key")
    if not secret_path.is_file() or secret_path.stat().st_size <= 0:
        raise MigrationError("迁移包会话密钥文件损坏：secret.key")
    if secret_path.stat().st_size > MAX_SECRET_BYTES:
        raise MigrationError("迁移包会话密钥文件过大，已停止导入。")
    master = (data_root / "master.key").read_bytes()
    secret = (data_root / "secret.key").read_bytes()
    env_master = os.environ.get("OSTENSORY_MASTER_KEY", "").strip()
    if env_master:
        try:
            env_bytes = bytes.fromhex(env_master)
        except ValueError as exc:
            raise MigrationError("当前环境的 OSTENSORY_MASTER_KEY 不是合法十六进制。") from exc
        if env_bytes != master:
            raise MigrationError(
                "当前环境设置了 OSTENSORY_MASTER_KEY，且与迁移包内主密钥不一致。"
                "请先移除或改回迁移包对应的主密钥，再导入，否则加密图片无法解密。"
            )
    return manifest, checked, total_bytes


_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _usable_password_hash(value: Any) -> bool:
    """Check the stored scrypt format without running an expensive hash.

    A package with an administrator row whose hash is malformed would import
    successfully and then lock the operator out.  This is a format check only;
    the password itself is still verified by the normal login path.
    """

    try:
        scheme, n_text, r_text, p_text, salt_text, digest_text = str(value).split("$")
        if scheme != "scrypt":
            return False
        if int(n_text) < 2 or int(r_text) < 1 or int(p_text) < 1:
            return False
        if len(salt_text) > 128 or len(digest_text) > 256:
            return False
        if not _B64URL_RE.fullmatch(salt_text) or not _B64URL_RE.fullmatch(digest_text):
            return False
        salt = base64.urlsafe_b64decode(salt_text + "=" * (-len(salt_text) % 4))
        digest = base64.urlsafe_b64decode(digest_text + "=" * (-len(digest_text) % 4))
        return len(salt) >= 8 and len(digest) >= 16
    except (TypeError, ValueError):
        return False


def _validate_blob_files(conn: sqlite3.Connection, data_root: Path) -> None:
    rows = conn.execute("SELECT * FROM blobs ORDER BY id").fetchall()
    for row in rows:
        for field_name in ("storage_key", "thumb_key", "preview_key"):
            key = str(row[field_name] or "")
            if not key:
                if field_name == "storage_key":
                    raise MigrationError(f"图片 blob {row['id']} 缺少主文件。")
                continue
            if key.startswith(REMOTE_PREFIX):
                raise MigrationError(
                    "迁移包仍包含远程存储引用。请使用当前版本重新导出。"
                )
            relative = _validate_blob_key(key, label="图片存储路径")
            path = data_root / relative
            if not path.is_file():
                raise MigrationError(f"迁移包缺少图片文件：{relative}")
            if path.stat().st_size == 0:
                raise MigrationError(f"迁移包图片文件为空：{relative}")


def prepare_import(config: Config, archive: Path) -> ImportPlan:
    archive = archive.resolve()
    max_size = max_archive_bytes()
    try:
        archive_size = archive.stat().st_size
    except OSError as exc:
        raise MigrationError(f"无法读取上传的迁移包：{exc}") from exc
    if archive_size <= 0:
        raise MigrationError("上传的迁移包是空文件。")
    if max_size and archive_size > max_size:
        raise MigrationError(
            f"迁移包超过大小限制（{max_size // MB} MB）。"
        )
    root = Path(tempfile.mkdtemp(prefix=".ostensory-migration-import-", dir=staging_dir(config)))
    try:
        _extract_archive(archive, root)
        extracted = root / "extracted"
        data_root = extracted / "data"
        manifest, files, total_bytes = _validate_manifest(
            root, extracted / MANIFEST_NAME, data_root, config
        )
        _sqlite_integrity(data_root / DB_NAME)
        conn = sqlite3.connect(str(data_root / DB_NAME), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            _validate_blob_files(conn, data_root)
            counts = _database_counts(conn)
            admin_row = conn.execute(
                "SELECT value FROM settings WHERE key = 'admin_path'"
            ).fetchone()
            _validate_admin_path(str((admin_row["value"] if admin_row else "admin") or "admin"))
            # Refuse a package that would leave the new instance with no usable
            # administrator.  Otherwise a corrupted/empty package could lock the
            # operator out immediately after a successful import.
            admin_rows = conn.execute(
                "SELECT username, password_hash FROM users "
                "WHERE role = 'admin' AND is_active = 1 AND username != ''"
            ).fetchall()
            if not any(_usable_password_hash(row["password_hash"]) for row in admin_rows):
                raise MigrationError(
                    "迁移包内没有可登录的管理员账户（密码哈希格式无效），已停止导入。"
                )
        finally:
            conn.close()
        # Opening the packaged SQLite file can create sidecars.  They are not
        # part of the manifest and must never be moved into the live data
        # directory (the archive itself rejects packaged sidecars).
        for suffix in ("-wal", "-shm", "-journal"):
            (data_root / f"{DB_NAME}{suffix}").unlink(missing_ok=True)
        master = (data_root / "master.key").read_bytes()
        secret = (data_root / "secret.key").read_bytes()
        needed = data_usage_bytes(config.data_dir) + total_bytes + 64 * MB
        _require_disk_space(config.data_dir, needed, "导入数据")
        return ImportPlan(
            root=root,
            data_dir=data_root,
            manifest=manifest,
            counts=counts,
            files=files,
            total_bytes=total_bytes,
            master_key=master,
            secret_key=secret,
            remote_localized=int(manifest.get("remote_localized") or 0),
        )
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise


def _copy_tree(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for entry in source.iterdir():
        target = destination / entry.name
        if _is_linkish(entry):
            raise MigrationError(f"备份目录包含符号链接，已停止：{entry}")
        if entry.is_dir():
            _copy_tree(entry, target)
        elif entry.is_file():
            shutil.copy2(entry, target)
        else:
            raise MigrationError(f"备份目录包含特殊文件，已停止：{entry}")


def _backup_roots(config: Config) -> list[Path]:
    roots: list[Path] = []
    configured = os.environ.get("OSTENSORY_MIGRATION_STATE_DIR", "").strip()
    if configured:
        # An explicit state directory is the operator's persistent backup
        # location.  Prefer it over the data directory's parent, which may be
        # the ephemeral container layer even when it happens to be writable.
        configured_root = Path(configured).resolve()
        if configured_root != config.data_dir:
            roots.append(configured_root)
    if config.data_dir.parent not in roots:
        roots.append(config.data_dir.parent)
    default_fallback = (config.server_dir / "migration-backups").resolve()
    if default_fallback not in roots and default_fallback != config.data_dir:
        roots.append(default_fallback)
    return roots


def _create_current_backup(config: Config, db: sqlite3.Connection) -> Path:
    last_error: OSError | None = None
    for root in _backup_roots(config):
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            last_error = exc
            continue
        backup = root / (
            f"{config.data_dir.name}.pre-migration-{_stamp()}-{uuid.uuid4().hex[:6]}"
        )
        if _inside(config.data_dir, backup):
            last_error = OSError("迁移备份目录不能位于数据目录内部")
            continue
        try:
            _copy_current_backup(config, db, backup)
            return backup
        except MigrationError:
            shutil.rmtree(backup, ignore_errors=True)
            raise
        except OSError as exc:
            shutil.rmtree(backup, ignore_errors=True)
            if exc.errno in {
                errno.EACCES,
                errno.EPERM,
                errno.EROFS,
                errno.ENOSPC,
                getattr(errno, "EDQUOT", errno.ENOSPC),
            }:
                last_error = exc
                continue
            raise
        except Exception:
            shutil.rmtree(backup, ignore_errors=True)
            raise
        except BaseException:
            # Cancellation must not leave a partial backup containing keys and
            # account data behind without a marker.
            shutil.rmtree(backup, ignore_errors=True)
            raise
    raise MigrationError(
        "无法创建导入前的完整备份，已取消导入。"
        "请确认数据目录的上级目录或 OSTENSORY_MIGRATION_STATE_DIR 可写且空间充足。"
    ) from last_error


def _copy_current_backup(config: Config, db: sqlite3.Connection, backup: Path) -> None:
    backup.mkdir(parents=True, exist_ok=False)
    try:
        backup.chmod(0o700)
    except OSError:
        pass
    target = sqlite3.connect(str(backup / DB_NAME))
    try:
        db.commit()
        db.backup(target)
        # Keep the recovery copy as one self-contained database file.  The
        # source is WAL-mode, and the backup target can inherit that flag.
        target.execute("PRAGMA journal_mode=DELETE")
    finally:
        target.close()
    for suffix in ("-wal", "-shm", "-journal"):
        (backup / f"{DB_NAME}{suffix}").unlink(missing_ok=True)
    try:
        (backup / DB_NAME).chmod(0o600)
    except OSError:
        pass
    for entry in config.data_dir.iterdir():
        name = entry.name
        if _is_excluded(name, for_backup=True):
            continue
        if _is_linkish(entry):
            raise MigrationError(f"数据目录包含符号链接，无法安全备份：{entry}")
        destination = backup / name
        if entry.is_dir():
            _copy_tree(entry, destination)
        elif entry.is_file():
            shutil.copy2(entry, destination)
        else:
            raise MigrationError(f"数据目录包含不支持的特殊文件，无法安全备份：{entry}")
    # Store the keys that are actually effective at runtime.  With env-provided
    # keys the files in data/ may be absent or stale, but a rollback must still
    # be able to reopen the old database and decrypt the old images.
    _write_key(backup / "master.key", config.master_key)
    _write_key(backup / "secret.key", config.secret_key)


def _clear_data_dir(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    for entry in data_dir.iterdir():
        if entry.is_dir() and not _is_linkish(entry):
            shutil.rmtree(entry)
        else:
            entry.unlink(missing_ok=True)


def _move_stage_data(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for entry in source.iterdir():
        target = destination / entry.name
        try:
            os.replace(entry, target)
        except OSError:
            if entry.is_dir():
                _copy_tree(entry, target)
                shutil.rmtree(entry)
            else:
                shutil.copy2(entry, target)
                entry.unlink()


def _reload_runtime(app, config: Config) -> sqlite3.Connection:
    master_env = os.environ.get("OSTENSORY_MASTER_KEY", "").strip()
    if master_env:
        master = bytes.fromhex(master_env)
        if len(master) != 32:
            raise RuntimeError("OSTENSORY_MASTER_KEY 必须是 32 字节")
    else:
        master = (config.data_dir / "master.key").read_bytes()
    secret_env = os.environ.get("OSTENSORY_SECRET_KEY")
    secret = (
        secret_env.encode("utf-8")
        if secret_env
        else (config.data_dir / "secret.key").read_bytes()
    )
    config.master_key = master
    config.secret_key = secret
    old_geo = getattr(app.state, "geo", None)
    if old_geo is not None:
        old_geo.close()
    init_db(config)
    app.state.storage = Storage(config)
    app.state.geo = GeoResolver(config)
    conn = connect(config.db_path)
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = 'admin_path'").fetchone()
        raw_path = row["value"] if row else "admin"
        admin_path = str(raw_path or "admin").strip().strip("/") or "admin"
        app.state.admin_path = admin_path
        language_row = conn.execute(
            "SELECT value FROM settings WHERE key = 'default_language'"
        ).fetchone()
        language_value = str(language_row["value"]) if language_row else "auto"
        app.state.default_language = (
            language_value if language_value in {"auto", "zh", "en"} else "auto"
        )
        theme_row = conn.execute(
            "SELECT value FROM settings WHERE key = 'default_theme'"
        ).fetchone()
        theme_value = str(theme_row["value"]) if theme_row else "dark"
        app.state.default_theme = (
            theme_value if theme_value in {"dark", "light", "auto"} else "dark"
        )
    finally:
        conn.close()
    return connect(config.db_path)


def _restore_backup(config: Config, backup: Path) -> None:
    _clear_data_dir(config.data_dir)
    _copy_tree(backup, config.data_dir)
    try:
        (config.data_dir).chmod(0o700)
    except OSError:
        pass


def apply_import(
    app,
    config: Config,
    db: sqlite3.Connection,
    plan: ImportPlan,
    *,
    actor: str = "",
) -> ImportResult:
    """Replace live data with the validated package.

    The caller must hold the migration gate.  A complete copy of the current
    data is created beside the data directory before anything is removed.  A
    marker outside the data directory allows the next startup to restore that
    backup if the process dies mid-apply.
    """

    backup = _create_current_backup(config, db)
    marker_root = backup.parent
    marker_written = False
    committed = False
    db_closed = False
    old_geo = getattr(app.state, "geo", None)
    try:
        _sync_filesystem()
        write_marker(config.data_dir, backup, marker_root)
        marker_written = True
        if old_geo is not None:
            old_geo.close()
        try:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        db.close()
        db_closed = True
        _clear_data_dir(config.data_dir)
        _move_stage_data(plan.data_dir, config.data_dir)
        try:
            config.data_dir.chmod(0o700)
        except OSError:
            pass
        new_db = None
        try:
            new_db = _reload_runtime(app, config)
            # Never carry logged-in browser sessions across machines.  The
            # administrator may still have a cookie from the old instance;
            # all sessions (including any inside the package) are invalidated.
            new_db.execute("DELETE FROM sessions")
            new_db.execute("DELETE FROM login_throttle")
            set_setting(new_db, UPDATE_LAST_IMPORT, utc_now())
            set_setting(new_db, UPDATE_LAST_BACKUP, str(backup))
            set_setting(
                new_db,
                "migration_imported_from_version",
                str(plan.manifest.get("app_version") or ""),
            )
            new_db.execute(
                "INSERT INTO audit_log(created_at, actor, action, target, detail, ip) "
                "VALUES(?,?,?,?,?,?)",
                (
                    utc_now(),
                    actor[:80],
                    "migration.import",
                    "data",
                    f"备份：{backup}; 文件：{len(plan.files)}",
                    "",
                ),
            )
            new_db.commit()
        finally:
            if new_db is not None:
                new_db.close()
        _sync_filesystem()
        result = ImportResult(
            backup_path=backup,
            admin_path=app.state.admin_path,
            counts=plan.counts,
            files=len(plan.files),
            remote_localized=plan.remote_localized,
        )
        for name in KEY_NAMES:
            try:
                (config.data_dir / name).chmod(0o600)
            except OSError:
                pass
        for name in (DB_NAME,):
            try:
                (config.data_dir / name).chmod(0o600)
            except OSError:
                pass
        # This is the commit point.  Everything the new instance needs is
        # complete and synced; removing the marker is the last mutating action.
        remove_marker(config.data_dir, marker_root)
        committed = True
        marker_written = False
        return result
    except BaseException as exc:
        if committed:
            # The marker is already gone and the new data is complete.  Do not
            # roll back (and risk leaving partial data with no recovery marker)
            # just because of an interruption after the commit point.
            raise
        # Best effort: restore from the complete backup.  If that also fails,
        # keep the marker so the next startup retries before serving requests.
        # BaseException includes KeyboardInterrupt/SystemExit: a cancellation
        # in the destructive window must still roll back before the process
        # leaves the maintenance gate.  Keep the instance out of service until
        # the rollback has fully succeeded.
        app.state.migration_rolling_back = True
        try:
            try:
                marker_present = marker_written or marker_path(
                    config.data_dir, marker_root
                ).exists()
            except OSError:
                # Conservatively treat an unreadable marker state as present;
                # leaving a backup is safer than deleting the only recovery copy.
                marker_present = True
            if db_closed:
                current_geo = getattr(app.state, "geo", None)
                if current_geo is not None:
                    current_geo.close()
                _restore_backup(config, backup)
                _sync_filesystem()
                _reload_runtime(app, config).close()
            elif marker_present:
                # No data was cleared yet, but the old GeoIP handle may already
                # have been closed; rebuild the runtime from the untouched data.
                _reload_runtime(app, config).close()
            else:
                # Nothing was destroyed yet; remove the partial backup.
                shutil.rmtree(backup, ignore_errors=True)
            if marker_present:
                remove_marker(config.data_dir, marker_root)
                marker_written = False
            app.state.migration_rolling_back = False
        except Exception:
            # The live directory may be partial.  Keep it out of service until
            # the operator restarts and startup recovery can prove/restore the
            # backup instead of serving an ambiguous database.
            app.state.migration_rolling_back = False
            app.state.migration_fatal = True
            if not isinstance(exc, Exception):
                raise exc
            raise MigrationError(
                f"导入失败，自动回滚也失败。服务将保持不可用；请保留备份并在重启前手动恢复：{backup}"
            ) from exc
        if not isinstance(exc, Exception):
            raise
        if isinstance(exc, MigrationError):
            raise
        raise MigrationError(f"导入失败，已恢复原数据：{exc}") from exc
    finally:
        if not db_closed:
            try:
                db.close()
            except Exception:
                pass