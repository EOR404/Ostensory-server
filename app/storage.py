from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from PIL import Image, ImageOps, UnidentifiedImageError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import Config
from .secretbox import decrypt_config

Image.MAX_IMAGE_PIXELS = 120_000_000

ENCRYPTION_HEADER = b"CBED1"
REMOTE_PREFIX = "remote/"
MB = 1024 * 1024
MIME_BY_FORMAT = {
    "PNG": ("png", "image/png"),
    "JPEG": ("jpg", "image/jpeg"),
    "WEBP": ("webp", "image/webp"),
    "GIF": ("gif", "image/gif"),
}
ALLOWED_FORMATS = set(MIME_BY_FORMAT)
CHUNK = 1024 * 1024


class StorageError(Exception):
    pass


class StorageFull(StorageError):
    pass


class UploadTooLarge(StorageError):
    pass


class UnsupportedImage(StorageError):
    pass


@dataclass
class BlobInfo:
    sha256: str
    storage_key: str
    thumb_key: str
    preview_key: str
    encrypted: bool
    size: int
    mime: str
    width: int
    height: int
    existing: bool
    backend_id: int = 1


class Storage:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.root = config.data_dir
        self.tmp_dir = config.data_dir / "tmp"
        self.cache_dir = config.data_dir / "cache"
        for name in ("originals", "thumbs", "previews", "tmp", "cache"):
            (config.data_dir / name).mkdir(parents=True, exist_ok=True)
        self._backend_rows: list[sqlite3.Row] = []
        self._backends_loaded = False

    # -- paths ---------------------------------------------------------
    def path_for(self, key: str | None) -> Path | None:
        if not key:
            return None
        backend_id, inner = self._split_key(key)
        if backend_id is None:
            candidate = (self.root / key).resolve()
            root = self.root.resolve()
            if not candidate.is_relative_to(root):
                raise StorageError("非法存储路径")
            return candidate
        return self._remote_path_for(backend_id, inner)

    def _relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root.resolve()).as_posix()

    def _split_key(self, key: str) -> tuple[int | None, str]:
        if key.startswith(REMOTE_PREFIX):
            parts = key.split("/", 2)
            if len(parts) == 3 and parts[1].isdigit():
                return int(parts[1]), parts[2]
        return None, key

    def _remote_key(self, backend_id: int, key: str) -> str:
        return f"{REMOTE_PREFIX}{backend_id}/{key}"

    def _cache_path(self, backend_id: int, key: str) -> Path:
        candidate = (self.cache_dir / str(backend_id) / key).resolve()
        cache_root = self.cache_dir.resolve()
        if not candidate.is_relative_to(cache_root):
            raise StorageError("非法存储路径")
        return candidate

    # -- backend registry ---------------------------------------------
    def _read_backend_rows(self) -> list[sqlite3.Row]:
        try:
            conn = sqlite3.connect(str(self.config.db_path), timeout=10)
            conn.row_factory = sqlite3.Row
            try:
                return conn.execute(
                    "SELECT * FROM storage_backends ORDER BY priority, id"
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            return []

    def reload_backends(self) -> None:
        self._backend_rows = self._read_backend_rows()
        self._backends_loaded = True

    def backend_rows(self) -> list[sqlite3.Row]:
        if not self._backends_loaded:
            self.reload_backends()
        return self._backend_rows

    def _backend_row(self, backend_id: int) -> sqlite3.Row | None:
        for row in self.backend_rows():
            if int(row["id"]) == backend_id:
                return row
        # A newly added backend may not be in the cache yet.
        self.reload_backends()
        for row in self._backend_rows:
            if int(row["id"]) == backend_id:
                return row
        return None

    def _backend(self, row: sqlite3.Row):
        from .storage_backends import build_backend

        try:
            config = json.loads(row["config_json"] or "{}")
        except (ValueError, TypeError) as exc:
            raise StorageError("存储后端配置不是合法 JSON") from exc
        if not isinstance(config, dict):
            config = {}
        try:
            config = decrypt_config(self.config.master_key, config)
        except ValueError as exc:
            raise StorageError(str(exc)) from exc
        return build_backend(str(row["kind"]), config)

    def backend_usage(self, db: sqlite3.Connection, backend_id: int) -> int:
        row = db.execute(
            "SELECT COALESCE(SUM(size), 0) AS n FROM blobs WHERE backend_id = ?",
            (backend_id,),
        ).fetchone()
        return int(row["n"] if row else 0)

    def capacity_status(
        self, db: sqlite3.Connection, incoming_size: int = 0
    ) -> dict[str, Any]:
        """Describe whether the enabled backends can accept more bytes.

        The numbers are deliberately about this application's own blobs, not a
        cloud account's total free space.  A quota of zero means unlimited.
        """

        wanted = max(0, min(2 * 1024 * 1024 * 1024, int(incoming_size or 0)))
        rows = db.execute(
            "SELECT * FROM storage_backends WHERE enabled = 1 ORDER BY priority, id"
        ).fetchall()
        backends: list[dict[str, Any]] = []
        any_ok = False
        unlimited = False
        available_total = 0
        for row in rows:
            quota_mb = max(0, int(row["quota_mb"] or 0))
            threshold = max(1, min(100, int(row["threshold_pct"] or 90)))
            used = self.backend_usage(db, int(row["id"]))
            if quota_mb == 0:
                available = None
                ok = True
                unlimited = True
            else:
                quota = quota_mb * MB
                limit = quota * threshold // 100
                available = max(0, min(quota, limit) - used)
                ok = used * 100 < quota * threshold and used + wanted <= quota
                available_total += available
            if ok:
                any_ok = True
            backends.append(
                {
                    "id": int(row["id"]),
                    "kind": str(row["kind"]),
                    "name": str(row["name"]),
                    "used_bytes": used,
                    "quota_bytes": quota_mb * MB,
                    "threshold_pct": threshold,
                    "available_bytes": available,
                    "ok": ok,
                }
            )
        if unlimited:
            available_total_value: int | None = None
        else:
            available_total_value = available_total
        return {
            "ok": any_ok,
            "requested_bytes": wanted,
            "available_bytes": available_total_value,
            "backends": backends,
            "reason": "ok" if any_ok else "full",
        }

    def _candidate_backends(
        self, db: sqlite3.Connection | None, incoming_size: int
    ) -> list[sqlite3.Row]:
        if db is None:
            return []
        rows = [
            row
            for row in db.execute(
                "SELECT * FROM storage_backends WHERE enabled = 1 ORDER BY priority, id"
            ).fetchall()
        ]
        result: list[sqlite3.Row] = []
        for row in rows:
            quota_mb = max(0, int(row["quota_mb"] or 0))
            threshold = max(1, min(100, int(row["threshold_pct"] or 90)))
            used = self.backend_usage(db, int(row["id"]))
            if quota_mb:
                quota = quota_mb * MB
                if used * 100 >= quota * threshold:
                    continue
                if used + incoming_size > quota:
                    continue
            result.append(row)
        return result

    def _remote_path_for(self, backend_id: int, key: str) -> Path | None:
        cache_path = self._cache_path(backend_id, key)
        if cache_path.exists():
            return cache_path
        row = self._backend_row(backend_id)
        if not row:
            return None
        try:
            backend = self._backend(row)
            backend.get_to_file(key, cache_path)
            return cache_path
        except Exception:
            return None

    # -- ingest --------------------------------------------------------
    def ingest(
        self,
        source: BinaryIO,
        filename: str,
        allowed_extensions: set[str],
        max_bytes: int,
        db: sqlite3.Connection | None = None,
    ) -> BlobInfo:
        suffix = Path(filename or "upload").suffix.lower().lstrip(".")
        if suffix == "jpeg":
            suffix = "jpg"
        if suffix not in allowed_extensions:
            raise UnsupportedImage(f"不支持的扩展名：.{suffix or '未知'}")

        fd, temp_name = tempfile.mkstemp(
            prefix="upload-", suffix=".part", dir=self.tmp_dir
        )
        temp_path = Path(temp_name)
        derive_dir = self.tmp_dir / f"derive-{uuid.uuid4().hex}"
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(fd, "wb") as out:
                while True:
                    chunk = source.read(CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise UploadTooLarge(
                            f"文件超过上限（{max_bytes // 1024 // 1024} MB）"
                        )
                    digest.update(chunk)
                    out.write(chunk)
            if size == 0:
                raise UnsupportedImage("空文件")
            sha = digest.hexdigest()
            width, height, fmt, mime, canonical_ext = self._probe(temp_path)
            if fmt not in ALLOWED_FORMATS:
                raise UnsupportedImage(f"不支持的图片格式：{fmt}")
            if canonical_ext not in allowed_extensions:
                raise UnsupportedImage(f"服务器不允许 .{canonical_ext} 格式")

            storage_key = f"originals/{sha[:2]}/{sha}.{canonical_ext}"
            thumb_key = f"thumbs/{sha[:2]}/{sha}.webp"
            preview_key = f"previews/{sha[:2]}/{sha}.webp"
            derive_dir.mkdir(parents=True, exist_ok=True)
            thumb_temp = derive_dir / "thumb.webp"
            preview_temp = derive_dir / "preview.webp"
            self._make_derivatives(temp_path, thumb_temp, preview_temp)

            candidates = self._candidate_backends(db, size)
            if not candidates:
                if db is not None:
                    raise StorageFull(
                        "存储空间不足：所有可用的存储方式都已停用、满载或达到使用率阈值"
                    )
                # No database context (CLI/tests): keep backwards-compatible
                # local-only behavior.
                return self._store_local(
                    1, temp_path, thumb_temp, preview_temp,
                    sha, storage_key, thumb_key, preview_key,
                    size, mime, width, height,
                )

            errors: list[str] = []
            for row in candidates:
                backend_id = int(row["id"])
                kind = str(row["kind"])
                try:
                    if kind == "local":
                        return self._store_local(
                            backend_id, temp_path, thumb_temp, preview_temp,
                            sha, storage_key, thumb_key, preview_key,
                            size, mime, width, height,
                        )
                    return self._store_remote(
                        row, temp_path, thumb_temp, preview_temp,
                        sha, storage_key, thumb_key, preview_key,
                        size, mime, width, height,
                    )
                except Exception as exc:
                    errors.append(f"{row['name']}: {exc}")
                    continue
            raise StorageError("存储后端写入失败：" + "；".join(errors[-3:]))
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise
        finally:
            temp_path.unlink(missing_ok=True)
            shutil.rmtree(derive_dir, ignore_errors=True)

    def _store_local(
        self,
        backend_id: int,
        temp_path: Path,
        thumb_temp: Path,
        preview_temp: Path,
        sha: str,
        storage_key: str,
        thumb_key: str,
        preview_key: str,
        size: int,
        mime: str,
        width: int,
        height: int,
    ) -> BlobInfo:
        final_path = self.path_for(storage_key)
        assert final_path is not None
        final_path.parent.mkdir(parents=True, exist_ok=True)
        existing = final_path.exists()
        if not existing:
            shutil.copy2(temp_path, final_path)
        thumb_path = self.path_for(thumb_key)
        preview_path = self.path_for(preview_key)
        assert thumb_path is not None and preview_path is not None
        if not thumb_path.exists():
            thumb_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(thumb_temp, thumb_path)
        if not preview_path.exists():
            preview_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(preview_temp, preview_path)
        return BlobInfo(
            sha256=sha,
            storage_key=storage_key,
            thumb_key=thumb_key,
            preview_key=preview_key,
            encrypted=False,
            size=size,
            mime=mime,
            width=width,
            height=height,
            existing=existing,
            backend_id=backend_id,
        )

    def _store_remote(
        self,
        row: sqlite3.Row,
        temp_path: Path,
        thumb_temp: Path,
        preview_temp: Path,
        sha: str,
        storage_key: str,
        thumb_key: str,
        preview_key: str,
        size: int,
        mime: str,
        width: int,
        height: int,
    ) -> BlobInfo:
        backend_id = int(row["id"])
        backend = self._backend(row)
        uploaded: list[str] = []
        try:
            for key, path, content_type in (
                (storage_key, temp_path, mime),
                (thumb_key, thumb_temp, "image/webp"),
                (preview_key, preview_temp, "image/webp"),
            ):
                if not backend.exists(key):
                    backend.put_file(key, path, content_type)
                    uploaded.append(key)
        except Exception:
            for key in uploaded:
                try:
                    backend.delete(key)
                except Exception:
                    pass
            raise
        return BlobInfo(
            sha256=sha,
            storage_key=self._remote_key(backend_id, storage_key),
            thumb_key=self._remote_key(backend_id, thumb_key),
            preview_key=self._remote_key(backend_id, preview_key),
            encrypted=False,
            size=size,
            mime=mime,
            width=width,
            height=height,
            existing=False,
            backend_id=backend_id,
        )

    def _probe(self, path: Path) -> tuple[int, int, str, str, str]:
        try:
            with Image.open(path) as image:
                fmt = (image.format or "").upper()
                width, height = image.size
                if fmt not in ALLOWED_FORMATS:
                    raise UnsupportedImage(f"不支持的图片格式：{fmt or '未知'}")
                image.verify()
            with Image.open(path) as image:
                if image.size != (width, height):
                    width, height = image.size
            ext, mime = MIME_BY_FORMAT[fmt]
            return width, height, fmt, mime, ext
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise UnsupportedImage("无法识别的图片文件") from exc

    def _make_derivatives(
        self, original: Path, thumb_path: Path, preview_path: Path
    ) -> None:
        thumb_path.parent.mkdir(parents=True, exist_ok=True)
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(original) as image:
            try:
                image.seek(0)
            except EOFError:
                pass
            image = ImageOps.exif_transpose(image)
            if image.mode not in ("RGB", "RGBA", "L"):
                image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            for target, max_side, quality in (
                (thumb_path, 640, 80),
                (preview_path, 1800, 86),
            ):
                work = image.copy()
                work.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
                if work.mode == "L":
                    work = work.convert("RGB")
                tmp = target.with_name(target.name + f".{uuid.uuid4().hex}.part")
                work.save(tmp, format="WEBP", quality=quality, method=4)
                os.replace(tmp, target)

    # -- encryption ----------------------------------------------------
    def _aes(self) -> AESGCM:
        return AESGCM(self.config.master_key)

    def encrypt_file(self, path: Path) -> None:
        if not path or not path.exists():
            return
        raw = path.read_bytes()
        if raw.startswith(ENCRYPTION_HEADER):
            return
        nonce = os.urandom(12)
        ciphertext = self._aes().encrypt(nonce, raw, ENCRYPTION_HEADER)
        tmp = path.with_suffix(path.suffix + ".encpart")
        tmp.write_bytes(ENCRYPTION_HEADER + nonce + ciphertext)
        os.replace(tmp, path)

    def decrypt_file_bytes(self, path: Path) -> bytes:
        raw = path.read_bytes()
        if not raw.startswith(ENCRYPTION_HEADER):
            return raw
        nonce = raw[len(ENCRYPTION_HEADER) : len(ENCRYPTION_HEADER) + 12]
        payload = raw[len(ENCRYPTION_HEADER) + 12 :]
        return self._aes().decrypt(nonce, payload, ENCRYPTION_HEADER)

    def decrypt_to_bytes(self, path: Path, encrypted: bool) -> bytes:
        if encrypted:
            return self.decrypt_file_bytes(path)
        return path.read_bytes()

    def is_encrypted_file(self, path: Path | None) -> bool:
        if not path or not path.exists():
            return False
        with path.open("rb") as handle:
            return handle.read(len(ENCRYPTION_HEADER)) == ENCRYPTION_HEADER

    def _transform_key(self, key: str, encrypt: bool) -> None:
        path = self.path_for(key)
        if not path or not path.exists():
            return
        raw = path.read_bytes()
        if encrypt:
            if raw.startswith(ENCRYPTION_HEADER):
                return
            nonce = os.urandom(12)
            ciphertext = self._aes().encrypt(nonce, raw, ENCRYPTION_HEADER)
            transformed = ENCRYPTION_HEADER + nonce + ciphertext
        else:
            if not raw.startswith(ENCRYPTION_HEADER):
                return
            transformed = self.decrypt_file_bytes(path)
        backend_id, inner = self._split_key(key)
        if backend_id is not None:
            row = self._backend_row(backend_id)
            if not row:
                raise StorageError("存储后端不存在")
            backend = self._backend(row)
            temp = path.with_name(path.name + f".{uuid.uuid4().hex}.part")
            temp.write_bytes(transformed)
            try:
                backend.put_file(inner, temp, "application/octet-stream")
            finally:
                temp.unlink(missing_ok=True)
        tmp = path.with_name(path.name + f".{uuid.uuid4().hex}.part")
        tmp.write_bytes(transformed)
        os.replace(tmp, path)

    def encrypt_blob_files(self, blob: sqlite3.Row) -> None:
        for key in (blob["storage_key"], blob["thumb_key"], blob["preview_key"]):
            if key:
                self._transform_key(key, True)

    def decrypt_blob_files(self, blob: sqlite3.Row) -> None:
        for key in (blob["storage_key"], blob["thumb_key"], blob["preview_key"]):
            if key:
                self._transform_key(key, False)

    def clone_blob_files(self, blob: sqlite3.Row) -> dict[str, str]:
        tag = uuid.uuid4().hex[:12]
        new_keys: dict[str, str] = {}
        for field_name, directory, suffix in (
            ("storage_key", "originals", Path(blob["storage_key"]).suffix),
            ("thumb_key", "thumbs", ".webp"),
            ("preview_key", "previews", ".webp"),
        ):
            old_key = blob[field_name]
            if not old_key:
                new_keys[field_name] = ""
                continue
            old_path = self.path_for(old_key)
            sha = blob["sha256"]
            new_inner = f"{directory}/{sha[:2]}/{sha}-{tag}{suffix}"
            backend_id, _ = self._split_key(old_key)
            if backend_id is None:
                new_key = new_inner
                new_path = self.path_for(new_key)
                assert old_path is not None and new_path is not None
                new_path.parent.mkdir(parents=True, exist_ok=True)
                if old_path.exists():
                    shutil.copy2(old_path, new_path)
            else:
                new_key = self._remote_key(backend_id, new_inner)
                row = self._backend_row(backend_id)
                if not row:
                    raise StorageError("存储后端不存在")
                backend = self._backend(row)
                if old_path and old_path.exists():
                    backend.put_file(new_inner, old_path, "application/octet-stream")
            new_keys[field_name] = new_key
        return new_keys

    def remove_keys(self, *keys: str | None) -> None:
        for key in keys:
            if not key:
                continue
            backend_id, inner = self._split_key(key)
            if backend_id is None:
                path = self.path_for(key)
                if path:
                    path.unlink(missing_ok=True)
                continue
            cache_path = self._cache_path(backend_id, inner)
            cache_path.unlink(missing_ok=True)
            cache_path.with_name(cache_path.name + ".part").unlink(missing_ok=True)
            row = self._backend_row(backend_id)
            if not row:
                continue
            try:
                self._backend(row).delete(inner)
            except Exception:
                # The DB row must still be removable if a remote provider is
                # temporarily unavailable; the orphan can be cleaned later.
                continue

    def remove_blob_files(self, blob: sqlite3.Row) -> None:
        self.remove_keys(
            blob["storage_key"], blob["thumb_key"], blob["preview_key"]
        )

    # -- read helpers --------------------------------------------------
    def read_file(self, key: str, encrypted: bool) -> io.BytesIO:
        path = self.path_for(key)
        if not path or not path.exists():
            raise FileNotFoundError(key)
        data = self.decrypt_to_bytes(path, encrypted)
        return io.BytesIO(data)

    def remove_upload_temp(self, filename: str) -> None:
        # UploadFile spools its own temp file; Starlette closes it. Kept for API symmetry.
        _ = filename