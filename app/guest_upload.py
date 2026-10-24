from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

from .config import Config
from .database import get_setting, set_setting
from .storage import Storage

KEY_MODES = {"off", "fixed", "daily", "once"}
CYCLE_SCOPES = {"album", "all"}
MB = 1024 * 1024
_DAILY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def normalized_mode(db: sqlite3.Connection) -> str:
    mode = (get_setting(db, "guest_upload_key_mode", "off") or "off").strip().lower()
    return mode if mode in KEY_MODES else "off"


def _digest(config: Config, value: str) -> str:
    return hmac.new(
        config.secret_key, ("guest-upload:" + value).encode("utf-8"), hashlib.sha256
    ).hexdigest()


def daily_key(config: Config, when: date | None = None) -> str:
    day = (when or datetime.now().astimezone().date()).isoformat()
    raw = hmac.new(
        config.secret_key, ("guest-upload-day:" + day).encode("utf-8"), hashlib.sha256
    ).digest()
    return base64.b32encode(raw[:5]).decode("ascii").rstrip("=")


def _normalize_code(value: str) -> str:
    return "".join(str(value or "").upper().split())


def verify_guest_key(
    db: sqlite3.Connection,
    config: Config,
    mode: str,
    provided: str,
    *,
    consume: bool = True,
) -> tuple[bool, str]:
    if mode == "off":
        return True, ""
    raw_code = str(provided or "").strip()
    if not raw_code:
        return False, "请填写访客上传密钥。"
    if mode == "fixed":
        stored = get_setting(db, "guest_upload_key_hash", "") or ""
        if not stored:
            return False, "管理员还没有设置固定密钥。"
        return (
            (True, "")
            if hmac.compare_digest(stored, _digest(config, raw_code))
            else (False, "上传密钥不正确。")
        )
    code = _normalize_code(raw_code)
    if mode == "daily":
        today = datetime.now().astimezone().date()
        accepted = {
            _normalize_code(daily_key(config, today)),
            _normalize_code(daily_key(config, today - timedelta(days=1))),
        }
        return (
            (True, "")
            if any(hmac.compare_digest(code, value) for value in accepted)
            else (False, "上传密钥不正确或已经过期。")
        )
    if mode == "once":
        if (get_setting(db, "guest_upload_once_used", "0") or "0") == "1":
            return False, "一次性密钥已经被使用，请联系管理员重新生成。"
        stored = get_setting(db, "guest_upload_once_hash", "") or ""
        if not stored:
            return False, "管理员还没有生成一次性密钥。"
        if not hmac.compare_digest(stored, _digest(config, code)):
            return False, "上传密钥不正确。"
        if consume:
            set_setting(db, "guest_upload_once_used", "1")
        return True, ""
    return False, "访客上传密钥配置不正确。"


def key_required(mode: str) -> bool:
    return mode != "off"


def generate_once_code(db: sqlite3.Connection, config: Config) -> str:
    code = "".join(secrets.choice(_DAILY_ALPHABET) for _ in range(10))
    set_setting(db, "guest_upload_once_code", code)
    set_setting(db, "guest_upload_once_hash", _digest(config, code))
    set_setting(db, "guest_upload_once_used", "0")
    return code


def set_fixed_key(db: sqlite3.Connection, config: Config, value: str) -> None:
    set_setting(db, "guest_upload_key_hash", _digest(config, value.strip()))


def allowed_album_ids(db: sqlite3.Connection) -> set[int]:
    raw = get_setting(db, "guest_upload_album_ids", "") or ""
    result: set[int] = set()
    for part in raw.replace("，", ",").split(","):
        part = part.strip()
        if part.isdigit() and int(part) > 0:
            result.add(int(part))
    return result


def album_allowed(db: sqlite3.Connection, album_id: int) -> bool:
    allowed = allowed_album_ids(db)
    return not allowed or album_id in allowed


def album_choices(db: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = db.execute("SELECT * FROM albums ORDER BY path COLLATE NOCASE").fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["depth"] = max(0, str(item.get("path") or "").count("/"))
        result.append(item)
    return result


def quota_bytes(db: sqlite3.Connection) -> int:
    row = db.execute(
        "SELECT COALESCE(SUM(b.size), 0) AS n FROM assets a "
        "JOIN blobs b ON b.id = a.blob_id "
        "WHERE a.source = 'guest' AND a.deleted_at IS NULL"
    ).fetchone()
    return int(row["n"] or 0) if row else 0


def cycle_enabled(db: sqlite3.Connection) -> bool:
    return (get_setting(db, "guest_upload_cycle_enabled", "0") or "0") == "1"


def cycle_scope(db: sqlite3.Connection) -> str:
    scope = (get_setting(db, "guest_upload_cycle_scope", "album") or "album").strip().lower()
    return scope if scope in CYCLE_SCOPES else "album"


def _row_value(row: Any, key: str, default: Any = 0) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def album_quota_mb(album: Any) -> int:
    """Per-album guest allowance in MB. 0 means no album-specific limit."""
    try:
        value = int(_row_value(album, "guest_quota_mb", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, min(value, 1024 * 1024))


def guest_album_bytes(db: sqlite3.Connection, album_id: int) -> int:
    row = db.execute(
        "SELECT COALESCE(SUM(b.size), 0) AS n FROM assets a "
        "JOIN blobs b ON b.id = a.blob_id "
        "WHERE a.source = 'guest' AND a.deleted_at IS NULL AND a.album_id = ?",
        (album_id,),
    ).fetchone()
    return int(row["n"] or 0) if row else 0


def guest_bytes_by_album(db: sqlite3.Connection) -> dict[int, int]:
    rows = db.execute(
        "SELECT a.album_id AS album_id, COALESCE(SUM(b.size), 0) AS n "
        "FROM assets a JOIN blobs b ON b.id = a.blob_id "
        "WHERE a.source = 'guest' AND a.deleted_at IS NULL "
        "GROUP BY a.album_id"
    ).fetchall()
    return {
        int(row["album_id"]): int(row["n"] or 0)
        for row in rows
        if row["album_id"] is not None
    }


def capacity_view(
    limit_mb: int, used_bytes: int, incoming_bytes: int = 0
) -> dict[str, Any]:
    """Aggregate one capacity pool for display and validation."""
    limit = max(0, int(limit_mb or 0)) * MB
    used = max(0, int(used_bytes or 0))
    incoming = max(0, int(incoming_bytes or 0))
    unlimited = limit == 0
    remaining = max(0, limit - used) if not unlimited else -1
    return {
        "limit_mb": int(limit_mb or 0),
        "used_mb": round(used / MB, 2),
        "remaining_mb": None if unlimited else round(remaining / MB, 2),
        "remaining_bytes": remaining,
        "unlimited": unlimited,
        "full": bool(not unlimited and used >= limit),
        "fits": bool(unlimited or used + incoming <= limit),
        "percent": (
            min(100, round(used * 100 / limit, 1)) if not unlimited else 0
        ),
    }


def guest_capacity_state(
    db: sqlite3.Connection,
    album_id: int,
    incoming_bytes: int = 0,
    total_quota_mb: int = 0,
) -> dict[str, Any]:
    album = db.execute(
        "SELECT id, name, guest_quota_mb FROM albums WHERE id = ?", (album_id,)
    ).fetchone()
    album_view = capacity_view(
        album_quota_mb(album) if album else 0,
        guest_album_bytes(db, album_id),
        incoming_bytes,
    )
    total_view = capacity_view(total_quota_mb, quota_bytes(db), incoming_bytes)
    return {
        "album": album_view,
        "total": total_view,
        # Album shortage always has to be solved by freeing that album; a total
        # shortage can use the administrator's configured album/all scope.
        "album_short": not album_view["fits"],
        "total_short": not total_view["fits"],
    }


def make_room_for_guest_upload(
    db: sqlite3.Connection,
    storage: Storage,
    album_id: int,
    incoming_bytes: int,
    total_quota_mb: int,
    scope: str = "album",
    exclude_ids: set[int] | None = None,
) -> list[dict[str, Any]]:
    """Permanently remove the oldest *guest* assets until the file fits.

    Only rows with ``source = 'guest'`` are ever selected.  Administrator or
    client uploads are neither counted nor eligible for deletion.  An album
    shortage is necessarily fixed from the target album; the configured scope
    only decides which guest images may be removed for a total-pool shortage.
    """
    from .services import delete_asset

    selected_scope = scope if scope in CYCLE_SCOPES else "album"
    excluded = {
        int(value) for value in (exclude_ids or set()) if str(value).isdigit()
    }
    exclude_sql = ""
    exclude_values: tuple[int, ...] = ()
    if excluded:
        exclude_sql = " AND a.id NOT IN (" + ",".join("?" for _ in excluded) + ")"
        exclude_values = tuple(sorted(excluded))
    deleted: list[dict[str, Any]] = []
    # Every iteration removes one active guest row, so the loop terminates even
    # when a large incoming file needs many old images removed.
    while True:
        state = guest_capacity_state(
            db, album_id, incoming_bytes, total_quota_mb
        )
        if not state["album_short"] and not state["total_short"]:
            break
        if state["album_short"]:
            row = db.execute(
                "SELECT a.id, a.original_name, a.album_id FROM assets a "
                "WHERE a.source = 'guest' AND a.deleted_at IS NULL "
                "AND a.album_id = ?" + exclude_sql + " "
                "ORDER BY a.created_at ASC, a.id ASC LIMIT 1",
                (album_id, *exclude_values),
            ).fetchone()
        elif selected_scope == "all":
            row = db.execute(
                "SELECT a.id, a.original_name, a.album_id FROM assets a "
                "WHERE a.source = 'guest' AND a.deleted_at IS NULL"
                + exclude_sql + " ORDER BY a.created_at ASC, a.id ASC LIMIT 1",
                exclude_values,
            ).fetchone()
        else:
            row = db.execute(
                "SELECT a.id, a.original_name, a.album_id FROM assets a "
                "WHERE a.source = 'guest' AND a.deleted_at IS NULL "
                "AND a.album_id = ?" + exclude_sql + " "
                "ORDER BY a.created_at ASC, a.id ASC LIMIT 1",
                (album_id, *exclude_values),
            ).fetchone()
        if not row:
            break
        asset_id = int(row["id"])
        delete_asset(db, storage, asset_id, purge=True)
        # A removed guest image may have been used as a cover.  Clear the stale
        # reference so public pages do not point at a purged asset.
        db.execute(
            "UPDATE albums SET cover_asset_id = NULL WHERE cover_asset_id = ?",
            (asset_id,),
        )
        db.execute(
            "UPDATE boxes SET cover_asset_id = NULL WHERE cover_asset_id = ?",
            (asset_id,),
        )
        deleted.append(
            {
                "id": asset_id,
                "original_name": str(row["original_name"] or ""),
                "album_id": row["album_id"],
            }
        )
    return deleted


def parse_titles(value: str) -> list[str]:
    try:
        data = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [str(item).strip()[:300] if isinstance(item, str) else "" for item in data]