from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta, timezone

from .services import delete_asset
from .storage import Storage

RETENTION_KEY = "trash_retention_days"


def _parse_datetime(value: str | None) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def retention_days(db: sqlite3.Connection) -> int:
    row = db.execute("SELECT value FROM settings WHERE key = ?", (RETENTION_KEY,)).fetchone()
    try:
        days = int(row["value"] if row else -1)
    except (TypeError, ValueError):
        days = -1
    # -1 means keep forever; 0 means the recycle bin is disabled.
    return max(-1, min(36500, days))


def cutoff_for(days: int) -> str | None:
    if days <= 0:
        return None
    return _iso(datetime.now(timezone.utc) - timedelta(days=max(1, days)))


def expiry_for(deleted_at: str | None, days: int) -> dict[str, object] | None:
    deleted = _parse_datetime(deleted_at)
    if deleted is None or days <= 0:
        return None
    expires = deleted + timedelta(days=days)
    remaining = (expires - datetime.now(timezone.utc)).total_seconds()
    return {
        "expires_at": _iso(expires),
        "days_left": max(0, int(math.ceil(remaining / 86400))),
        "expired": remaining <= 0,
    }


def stats(db: sqlite3.Connection) -> dict[str, int]:
    row = db.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(b.size), 0) AS bytes "
        "FROM assets a JOIN blobs b ON b.id = a.blob_id "
        "WHERE a.deleted_at IS NOT NULL"
    ).fetchone()
    exclusive = db.execute(
        "SELECT COALESCE(SUM(b.size), 0) AS n FROM blobs b "
        "WHERE EXISTS(SELECT 1 FROM assets a WHERE a.blob_id = b.id AND a.deleted_at IS NOT NULL) "
        "AND NOT EXISTS(SELECT 1 FROM assets a WHERE a.blob_id = b.id AND a.deleted_at IS NULL)"
    ).fetchone()
    return {
        "count": int(row["n"] if row else 0),
        "bytes": int(row["bytes"] if row else 0),
        "reclaimable_bytes": int(exclusive["n"] if exclusive else 0),
    }


def purge_expired(db: sqlite3.Connection, storage: Storage) -> dict[str, int]:
    days = retention_days(db)
    cutoff = cutoff_for(days)
    if not cutoff:
        return {"deleted": 0, "failed": 0}
    rows = db.execute(
        "SELECT id FROM assets WHERE deleted_at IS NOT NULL "
        "AND datetime(deleted_at) <= datetime(?) "
        "ORDER BY deleted_at ASC, id ASC LIMIT 5000",
        (cutoff,),
    ).fetchall()
    deleted = 0
    failed = 0
    for row in rows:
        try:
            delete_asset(db, storage, int(row["id"]), purge=True)
            db.commit()
            deleted += 1
        except Exception:
            db.rollback()
            failed += 1
    return {"deleted": deleted, "failed": failed}


def reclaim(
    db: sqlite3.Connection,
    storage: Storage,
    incoming_bytes: int = 0,
    max_items: int = 3000,
) -> dict[str, object]:
    """Purge the oldest recycle-bin images until an upload can fit.

    Only rows with ``deleted_at`` set are eligible.  Shared blobs are kept
    until their last asset disappears, so a single image purge does not always
    release the same number of bytes it reports in the table.
    """

    wanted = max(0, min(2 * 1024 * 1024 * 1024, int(incoming_bytes or 0)))
    status = storage.capacity_status(db, wanted)
    if status.get("ok"):
        return {
            "ok": True,
            "deleted": 0,
            "failed": 0,
            "freed_bytes": 0,
            "status": status,
        }
    before = stats(db)["reclaimable_bytes"]
    rows = db.execute(
        "SELECT id FROM assets WHERE deleted_at IS NOT NULL "
        "ORDER BY deleted_at ASC, id ASC LIMIT ?",
        (max(1, min(10000, max_items)),),
    ).fetchall()
    deleted = 0
    failed = 0
    for row in rows:
        try:
            delete_asset(db, storage, int(row["id"]), purge=True)
            db.commit()
            deleted += 1
        except Exception:
            db.rollback()
            failed += 1
            continue
        status = storage.capacity_status(db, wanted)
        if status.get("ok"):
            break
    after = stats(db)["reclaimable_bytes"]
    return {
        "ok": bool(status.get("ok")),
        "deleted": deleted,
        "failed": failed,
        "freed_bytes": max(0, before - after),
        "status": status,
    }