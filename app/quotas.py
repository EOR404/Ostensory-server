from __future__ import annotations

import sqlite3

from .database import get_setting
from .storage import MB, Storage

QUOTA_MODE_KEY = "user_quota_mode"
QUOTA_DEFAULT_MB_KEY = "user_quota_default_mb"
QUOTA_FULL_ACTION_KEY = "user_quota_full_action"


class QuotaExceeded(ValueError):
    """Raised before an upload when a user's own storage allowance is full."""


def _integer(value: object, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def quota_limit_mb(db: sqlite3.Connection, user_id: int) -> int:
    row = db.execute(
        "SELECT quota_mb FROM users WHERE id = ?", (int(user_id),)
    ).fetchone()
    if not row:
        return 0
    # 0 means unlimited. Existing installs are upgraded with 0 so adding the
    # feature cannot unexpectedly lock existing accounts out.
    return max(0, _integer(row["quota_mb"], 0))


def used_bytes(db: sqlite3.Connection, user_id: int) -> int:
    """Logical bytes owned by the account, including recycle-bin rows.

    Recycle-bin files still consume disk, so they still consume quota until
    they are permanently deleted. Shared/deduplicated blobs are counted once
    per owned asset; this is deliberately conservative and never lets a user
    exceed the physical allowance through duplicate uploads.
    """

    row = db.execute(
        "SELECT COALESCE(SUM(b.size), 0) AS n FROM assets a "
        "JOIN blobs b ON b.id = a.blob_id WHERE a.owner_id = ?",
        (int(user_id),),
    ).fetchone()
    return max(0, _integer(row["n"] if row else 0, 0))


def quota_status(db: sqlite3.Connection, user_id: int) -> dict[str, object]:
    limit_mb = quota_limit_mb(db, user_id)
    used = used_bytes(db, user_id)
    if limit_mb <= 0:
        return {
            "unlimited": True,
            "limit_mb": 0,
            "limit_bytes": 0,
            "used_bytes": used,
            "remaining_bytes": None,
            "percent": 0,
            "full": False,
        }
    limit_bytes = limit_mb * MB
    remaining = max(0, limit_bytes - used)
    percent = min(100, int((used * 100) // limit_bytes)) if limit_bytes else 100
    return {
        "unlimited": False,
        "limit_mb": limit_mb,
        "limit_bytes": limit_bytes,
        "used_bytes": used,
        "remaining_bytes": remaining,
        "percent": percent,
        "full": used >= limit_bytes,
    }


def default_quota_mb(db: sqlite3.Connection, storage: Storage | None) -> int:
    """Value assigned to a newly created account.

    ``elastic`` divides the currently available physical upload capacity by
    the number of active accounts. It is a snapshot taken at account creation;
    it does not silently rewrite existing accounts when usage changes.
    """

    mode = get_setting(db, QUOTA_MODE_KEY, "fixed").strip().lower()
    if mode == "elastic" and storage is not None:
        try:
            capacity = storage.capacity_status(db)
        except Exception:
            capacity = {}
        available = capacity.get("available_bytes")
        if available is None:
            return 0
        row = db.execute(
            "SELECT COUNT(*) AS n FROM users WHERE is_active = 1"
        ).fetchone()
        accounts = max(1, _integer(row["n"] if row else 1, 1))
        return max(1, int(available) // MB // accounts)
    return max(0, _integer(get_setting(db, QUOTA_DEFAULT_MB_KEY, "1024"), 1024))


def purge_user_trash(
    db: sqlite3.Connection,
    storage: Storage,
    user_id: int,
    *,
    limit: int = 2000,
) -> int:
    """Permanently remove this user's oldest recycle-bin rows.

    Only rows already in the recycle bin are eligible. Active images are never
    touched by this quota policy.
    """

    from .services import delete_asset

    rows = db.execute(
        "SELECT id FROM assets WHERE owner_id = ? AND deleted_at IS NOT NULL "
        "ORDER BY deleted_at ASC, id ASC LIMIT ?",
        (int(user_id), max(1, min(10000, int(limit)))),
    ).fetchall()
    deleted = 0
    for row in rows:
        try:
            delete_asset(db, storage, int(row["id"]), purge=True)
            db.commit()
            deleted += 1
        except Exception:
            db.rollback()
            continue
    return deleted


def ensure_user_quota(
    db: sqlite3.Connection,
    storage: Storage,
    user_id: int,
    incoming_bytes: int,
    *,
    pending_bytes: int = 0,
) -> None:
    """Reject an upload before it writes files when the user is over quota."""

    incoming = max(0, int(incoming_bytes or 0))
    pending = max(0, int(pending_bytes or 0))
    if incoming == 0:
        return
    limit_mb = quota_limit_mb(db, user_id)
    if limit_mb <= 0:
        return
    limit = limit_mb * MB
    used = used_bytes(db, user_id)
    if used + pending + incoming <= limit:
        return

    action = get_setting(db, QUOTA_FULL_ACTION_KEY, "reject").strip().lower()
    if action == "clean_trash":
        # Try to make room only from this account's recycle bin. A second
        # measurement is required because deleting an asset may not free a
        # byte when its blob is shared with an active image.
        purge_user_trash(db, storage, user_id)
        used = used_bytes(db, user_id)
        if used + pending + incoming <= limit:
            return

    remaining = max(0, limit - used - pending)
    raise QuotaExceeded(
        "个人空间不足：配额 "
        f"{limit_mb} MB，已用 {used / MB:.1f} MB，"
        f"本次需要 {incoming / MB:.1f} MB，剩余 {remaining / MB:.1f} MB。"
        "请删除作品或清空回收站，也可以联系管理员调整配额。"
    )