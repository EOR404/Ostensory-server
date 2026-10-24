from __future__ import annotations

import json
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any, BinaryIO

from .access import countries_json, validate_region_mode
from .database import get_setting, utc_now
from .metadata import lora_names, normalize_client_metadata, parse_image_metadata
from .quotas import QuotaExceeded, ensure_user_quota
from .storage import BlobInfo, Storage

CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_WORKFLOW_CACHE: dict[tuple[str, int, str], dict[str, Any] | None] = {}


def safe_display_name(name: str) -> str:
    cleaned = CONTROL_RE.sub("", Path(name or "image").name).strip()
    return (cleaned or "image")[:200]


def allowed_extensions(db: sqlite3.Connection) -> set[str]:
    raw = get_setting(db, "allowed_extensions", "png,jpg,jpeg,webp,gif")
    result = {item.strip().lower().lstrip(".") for item in raw.split(",") if item.strip()}
    if "jpeg" in result:
        result.add("jpg")
    return result


def max_upload_bytes(db: sqlite3.Connection) -> int:
    try:
        mb = int(get_setting(db, "max_upload_mb", "200"))
    except ValueError:
        mb = 200
    return max(1, min(2048, mb)) * 1024 * 1024


def _blob_from_info(
    db: sqlite3.Connection,
    info: BlobInfo,
    storage: Storage,
) -> sqlite3.Row:
    row = db.execute(
        "SELECT * FROM blobs WHERE sha256 = ? ORDER BY encrypted ASC, id LIMIT 1",
        (info.sha256,),
    ).fetchone()
    if row:
        if row["storage_key"] != info.storage_key:
            storage.remove_keys(
                info.storage_key, info.thumb_key, info.preview_key
            )
        return row
    cursor = db.execute(
        "INSERT INTO blobs(sha256, storage_key, thumb_key, preview_key, backend_id, encrypted, size, mime, width, height, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            info.sha256,
            info.storage_key,
            info.thumb_key,
            info.preview_key,
            info.backend_id,
            0,
            info.size,
            info.mime,
            info.width,
            info.height,
            utc_now(),
        ),
    )
    row = db.execute("SELECT * FROM blobs WHERE id = ?", (int(cursor.lastrowid),)).fetchone()
    assert row is not None
    return row


def create_asset_from_upload(
    db: sqlite3.Connection,
    storage: Storage,
    source: BinaryIO,
    filename: str,
    album_id: int | None,
    *,
    box_id: int | None = None,
    source_name: str = "web",
    upload_ip: str = "",
    client_path: str | None = None,
    overwrite: bool = True,
    nsfw: bool = False,
    region_mode: str = "inherit",
    region_countries: str = "",
    title: str = "",
    description: str = "",
    expected_sha256: str | None = None,
    metadata: dict[str, Any] | None = None,
    owner_id: int | None = None,
    author_name: str = "",
    status: str = "approved",
    visibility: str = "public",
    access_password_hash: str | None = None,
) -> tuple[sqlite3.Row, str]:
    if album_id is not None:
        album = db.execute("SELECT id FROM albums WHERE id = ?", (album_id,)).fetchone()
        if not album:
            raise ValueError("相册不存在")
    allowed = allowed_extensions(db)
    max_bytes = max_upload_bytes(db)
    preview_enabled = get_setting(db, "preview_enabled", "1") == "1"
    info = storage.ingest(
        source,
        filename,
        allowed,
        max_bytes,
        db=db,
        preview=preview_enabled,
    )
    if expected_sha256 and info.sha256.lower() != expected_sha256.lower():
        if not info.existing:
            storage.remove_keys(
                info.storage_key, info.thumb_key, info.preview_key
            )
        raise ValueError("客户端 SHA-256 与服务端计算结果不一致")

    # A client upload for the same (album, path) replaces the previous active
    # row. Check this before creating a blob row so a duplicate does not count
    # against the owner's quota a second time.
    outcome = "created"
    if client_path:
        existing = db.execute(
            "SELECT a.*, b.sha256 FROM assets a JOIN blobs b ON b.id = a.blob_id "
            "WHERE a.album_id = ? AND a.client_path = ? AND a.deleted_at IS NULL",
            (album_id, client_path),
        ).fetchone()
        if existing:
            if existing["sha256"] == info.sha256:
                if not info.existing:
                    storage.remove_keys(
                        info.storage_key, info.thumb_key, info.preview_key
                    )
                return existing, "duplicate"
            if overwrite:
                db.execute(
                    "UPDATE assets SET deleted_at = ?, updated_at = ? WHERE id = ?",
                    (utc_now(), utc_now(), existing["id"]),
                )
                outcome = "updated"
            else:
                outcome = "created"

    if owner_id is not None:
        try:
            ensure_user_quota(db, storage, int(owner_id), int(info.size or 0))
        except QuotaExceeded:
            if not info.existing:
                storage.remove_keys(
                    info.storage_key, info.thumb_key, info.preview_key
                )
            raise

    blob = _blob_from_info(db, info, storage)

    metadata_path = storage.path_for(info.storage_key)
    assert metadata_path is not None
    if metadata is not None:
        # The desktop client already parsed the PNG/EXIF metadata locally and
        # sent the normalized result with the original file.  Do not parse the
        # same bytes again on the server.
        metadata = normalize_client_metadata(metadata, info.width, info.height)
    elif blob["encrypted"] or storage.is_encrypted_file(metadata_path):
        temp = storage.path_for(
            f"tmp/parse-{uuid.uuid4().hex}{Path(info.storage_key).suffix}"
        )
        assert temp is not None
        temp.write_bytes(storage.decrypt_to_bytes(metadata_path, True))
        try:
            metadata = parse_image_metadata(temp, info.width, info.height)
        finally:
            temp.unlink(missing_ok=True)
    else:
        metadata = parse_image_metadata(metadata_path, info.width, info.height)
    if get_setting(db, "experimental_features", "0") != "1":
        region_mode = "inherit"
        region_countries = ""
    now = utc_now()
    cursor = db.execute(
        "INSERT INTO assets(blob_id, album_id, box_id, original_name, title, description, "
        "intro_allow_html, metadata_json, "
        "positive_prompt, negative_prompt, lora_names, nsfw, region_mode, region_countries, "
        "source, upload_ip, client_path, owner_id, author_name, status, visibility, "
        "access_password_hash, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            blob["id"],
            album_id,
            box_id,
            safe_display_name(filename),
            (title or safe_display_name(filename))[:300],
            description[:5000],
            0 if source_name in {"guest", "user"} else 1,
            json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
            str(metadata.get("positive_prompt", ""))[:200000],
            str(metadata.get("negative_prompt", ""))[:200000],
            lora_names(metadata)[:4000],
            1 if nsfw else 0,
            validate_region_mode(region_mode),
            countries_json(region_countries),
            source_name[:40],
            upload_ip.strip()[:64],
            client_path[:1000] if client_path else None,
            owner_id,
            (author_name or ("未知用户" if source_name == "guest" else "管理员"))[:80],
            status if status in {"approved", "pending", "rejected"} else "approved",
            visibility if visibility in {"public", "private"} else "public",
            access_password_hash,
            now,
            now,
        ),
    )
    asset_id = int(cursor.lastrowid)
    asset = db.execute(
        "SELECT a.*, b.sha256, b.storage_key, b.thumb_key, b.preview_key, b.encrypted, "
        "b.size, b.mime, b.width, b.height FROM assets a JOIN blobs b ON b.id = a.blob_id WHERE a.id = ?",
        (asset_id,),
    ).fetchone()
    assert asset is not None
    db.execute("UPDATE albums SET updated_at = ? WHERE id = ?", (now, album_id))
    return asset, outcome


def blob_ref_count(db: sqlite3.Connection, blob_id: int) -> int:
    row = db.execute("SELECT COUNT(*) AS n FROM assets WHERE blob_id = ?", (blob_id,)).fetchone()
    return int(row["n"])


def clone_blob_for_asset(db: sqlite3.Connection, storage: Storage, asset: sqlite3.Row) -> sqlite3.Row:
    blob = db.execute("SELECT * FROM blobs WHERE id = ?", (asset["blob_id"],)).fetchone()
    if not blob:
        raise ValueError("图片数据不存在")
    if blob_ref_count(db, blob["id"]) <= 1:
        return blob
    keys = storage.clone_blob_files(blob)
    cursor = db.execute(
        "INSERT INTO blobs(sha256, storage_key, thumb_key, preview_key, backend_id, encrypted, size, mime, width, height, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            blob["sha256"],
            keys["storage_key"],
            keys["thumb_key"],
            keys["preview_key"],
            int(blob["backend_id"] or 1),
            blob["encrypted"],
            blob["size"],
            blob["mime"],
            blob["width"],
            blob["height"],
            utc_now(),
        ),
    )
    new_id = int(cursor.lastrowid)
    db.execute("UPDATE assets SET blob_id = ? WHERE id = ?", (new_id, asset["id"]))
    return db.execute("SELECT * FROM blobs WHERE id = ?", (new_id,)).fetchone()  # type: ignore[return-value]


def set_asset_encryption(
    db: sqlite3.Connection,
    storage: Storage,
    asset: sqlite3.Row,
    encrypt: bool,
) -> None:
    blob = clone_blob_for_asset(db, storage, asset)
    if bool(blob["encrypted"]) == bool(encrypt):
        return
    if encrypt:
        storage.encrypt_blob_files(blob)
    else:
        storage.decrypt_blob_files(blob)
    db.execute("UPDATE blobs SET encrypted = ? WHERE id = ?", (1 if encrypt else 0, blob["id"]))


def set_album_encryption(
    db: sqlite3.Connection,
    storage: Storage,
    album_id: int,
    encrypt: bool,
) -> int:
    from .access import descendant_album_ids

    ids = descendant_album_ids(db, album_id)
    placeholders = ",".join("?" for _ in ids)
    rows = db.execute(
        f"SELECT a.*, b.encrypted AS blob_encrypted FROM assets a JOIN blobs b ON b.id = a.blob_id "
        f"WHERE a.album_id IN ({placeholders}) AND a.deleted_at IS NULL",
        ids,
    ).fetchall()
    count = 0
    for row in rows:
        set_asset_encryption(db, storage, row, encrypt)
        count += 1
    return count


def set_box_encryption(
    db: sqlite3.Connection,
    storage: Storage,
    box_id: int,
    encrypt: bool,
) -> int:
    """Encrypt direct box images and images in every album nested in the box."""
    rows = db.execute(
        "WITH RECURSIVE box_albums(id) AS ("
        " SELECT id FROM albums WHERE box_id = ?"
        " UNION SELECT a.id FROM albums a JOIN box_albums b ON a.parent_id = b.id"
        ") "
        "SELECT DISTINCT a.* FROM assets a "
        "WHERE a.deleted_at IS NULL AND "
        "(a.box_id = ? OR a.album_id IN (SELECT id FROM box_albums)) "
        "ORDER BY a.id",
        (box_id, box_id),
    ).fetchall()
    count = 0
    for row in rows:
        set_asset_encryption(db, storage, row, encrypt)
        count += 1
    return count


def delete_asset(
    db: sqlite3.Connection, storage: Storage, asset_id: int, purge: bool = False
) -> bool:
    """Delete an asset and report whether it was permanently removed.

    A retention value of 0 disables the recycle bin entirely.  In that mode a
    normal delete is forced into a permanent delete so no hidden files remain
    on disk.  -1 keeps recycle-bin items forever and a positive value uses the
    normal timed recycle-bin path.
    """

    asset = db.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if not asset:
        return False
    if not purge:
        retention = get_setting(db, "trash_retention_days", "-1").strip()
        if retention != "0":
            db.execute(
                "UPDATE assets SET deleted_at = ?, updated_at = ? WHERE id = ?",
                (utc_now(), utc_now(), asset_id),
            )
            return False
        purge = True
    blob_id = asset["blob_id"]
    db.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
    # A permanently removed image must not leave a dangling cover reference.
    db.execute("UPDATE albums SET cover_asset_id = NULL WHERE cover_asset_id = ?", (asset_id,))
    db.execute("UPDATE boxes SET cover_asset_id = NULL WHERE cover_asset_id = ?", (asset_id,))
    refs = blob_ref_count(db, blob_id)
    if refs == 0:
        blob = db.execute("SELECT * FROM blobs WHERE id = ?", (blob_id,)).fetchone()
        if blob:
            storage.remove_blob_files(blob)
            db.execute("DELETE FROM blobs WHERE id = ?", (blob_id,))
    return True


def extract_workflow(db: sqlite3.Connection, storage: Storage, asset: sqlite3.Row) -> dict[str, Any] | None:
    """Recover the workflow JSON for a client-parsed asset on demand.

    Sync clients intentionally send only normalized metadata, so raw workflow
    JSON is not stored for those uploads.  The original file still contains it;
    parse a temporary copy only when someone actually opens the detail page.
    """
    cache_key = (str(storage.root), int(asset["id"]), str(asset["updated_at"]))
    if cache_key in _WORKFLOW_CACHE:
        return _WORKFLOW_CACHE[cache_key]
    blob = db.execute("SELECT * FROM blobs WHERE id = ?", (asset["blob_id"],)).fetchone()
    if not blob:
        return None
    path = storage.path_for(blob["storage_key"])
    if not path or not path.exists():
        return None
    temp = storage.path_for(f"tmp/workflow-{uuid.uuid4().hex}{Path(blob['storage_key']).suffix}")
    if temp is None:
        return None
    try:
        encrypted = bool(blob["encrypted"]) or storage.is_encrypted_file(path)
        temp.write_bytes(storage.decrypt_to_bytes(path, encrypted))
        metadata = parse_image_metadata(temp, blob["width"], blob["height"])
    except (OSError, ValueError):
        return None
    finally:
        temp.unlink(missing_ok=True)
    workflow = (metadata.get("raw") or {}).get("workflow")
    result = workflow if isinstance(workflow, dict) else None
    if len(_WORKFLOW_CACHE) >= 64:
        _WORKFLOW_CACHE.pop(next(iter(_WORKFLOW_CACHE)), None)
    _WORKFLOW_CACHE[cache_key] = result
    return result


def reparse_asset(db: sqlite3.Connection, storage: Storage, asset: sqlite3.Row) -> dict[str, Any]:
    blob = db.execute("SELECT * FROM blobs WHERE id = ?", (asset["blob_id"],)).fetchone()
    if not blob:
        raise ValueError("图片数据不存在")
    path = storage.path_for(blob["storage_key"])
    if not path or not path.exists():
        raise FileNotFoundError("原图文件不存在")
    if blob["encrypted"] or storage.is_encrypted_file(path):
        temp = storage.path_for(blob["storage_key"] + ".parse" + Path(blob["storage_key"]).suffix)
        assert temp is not None
        temp.write_bytes(storage.decrypt_to_bytes(path, True))
        try:
            metadata = parse_image_metadata(temp, blob["width"], blob["height"])
        finally:
            temp.unlink(missing_ok=True)
    else:
        metadata = parse_image_metadata(path, blob["width"], blob["height"])
    db.execute(
        "UPDATE assets SET metadata_json = ?, positive_prompt = ?, negative_prompt = ?, "
        "lora_names = ?, updated_at = ? WHERE id = ?",
        (
            json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
            str(metadata.get("positive_prompt", ""))[:200000],
            str(metadata.get("negative_prompt", ""))[:200000],
            lora_names(metadata)[:4000],
            utc_now(),
            asset["id"],
        ),
    )
    return metadata