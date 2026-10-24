from __future__ import annotations

import sqlite3

from .database import get_setting, utc_now


def enabled(db: sqlite3.Connection) -> bool:
    return get_setting(db, "likes_views_enabled", "1") == "1"


def anonymous_likes_allowed(db: sqlite3.Connection) -> bool:
    return get_setting(db, "allow_anonymous_likes", "0") == "1"


def like_count(db: sqlite3.Connection, asset_id: int) -> int:
    row = db.execute(
        "SELECT COUNT(*) AS n FROM asset_likes WHERE asset_id = ?", (asset_id,)
    ).fetchone()
    return int(row["n"]) if row else 0


def liked_by(db: sqlite3.Connection, asset_id: int, actor_key: str) -> bool:
    return bool(
        db.execute(
            "SELECT 1 FROM asset_likes WHERE asset_id = ? AND actor_key = ?",
            (asset_id, actor_key),
        ).fetchone()
    )


def set_like(
    db: sqlite3.Connection,
    asset_id: int,
    actor_key: str,
    user_id: int | None,
    desired: bool | None = None,
) -> tuple[bool, int, bool]:
    """Set a like to an explicit state and report whether anything changed.

    A desired state makes retries and double-clicks idempotent: sending
    "liked=true" twice writes once. ``None`` retains the original toggle
    behaviour for non-JavaScript clients.
    """

    existing = db.execute(
        "SELECT id FROM asset_likes WHERE asset_id = ? AND actor_key = ?",
        (asset_id, actor_key),
    ).fetchone()
    target = (not bool(existing)) if desired is None else bool(desired)
    changed = target != bool(existing)
    if changed:
        if target:
            db.execute(
                "INSERT INTO asset_likes(asset_id, user_id, actor_key, created_at) "
                "VALUES(?,?,?,?)",
                (asset_id, user_id, actor_key, utc_now()),
            )
        else:
            db.execute("DELETE FROM asset_likes WHERE id = ?", (existing["id"],))
    return target, like_count(db, asset_id), changed


def toggle_like(
    db: sqlite3.Connection,
    asset_id: int,
    actor_key: str,
    user_id: int | None,
) -> tuple[bool, int]:
    liked, count, _ = set_like(db, asset_id, actor_key, user_id)
    return liked, count


def record_view(db: sqlite3.Connection, asset_id: int, actor_key: str) -> bool:
    """Count one view per browser/account and asset, without unbounded rows.

    The asset row keeps the running total; the marker table prevents a simple
    refresh loop from inflating it.  A cleared cookie can create a new marker,
    which is the same tradeoff as most lightweight gallery counters.
    """
    try:
        cursor = db.execute(
            "INSERT OR IGNORE INTO asset_view_seen(asset_id, actor_key, created_at) "
            "VALUES(?,?,?)",
            (asset_id, actor_key, utc_now()),
        )
    except sqlite3.IntegrityError:
        return False
    if cursor.rowcount:
        db.execute("UPDATE assets SET views = views + 1 WHERE id = ?", (asset_id,))
        return True
    return False


def _rank_rows(db: sqlite3.Connection, expression: str, limit: int) -> list[dict]:
    rows = db.execute(
        "SELECT a.id, a.title, a.original_name, b.thumb_key, b.sha256, "
        f"{expression} AS metric "
        "FROM assets a JOIN blobs b ON b.id = a.blob_id "
        "LEFT JOIN asset_likes l ON l.asset_id = a.id "
        "WHERE a.deleted_at IS NULL AND a.status = 'approved' "
        "AND a.visibility = 'public' "
        f"GROUP BY a.id HAVING metric > 0 ORDER BY metric DESC, a.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "title": row["title"] or row["original_name"],
            "thumb_key": row["thumb_key"],
            "sha256": row["sha256"],
            "count": int(row["metric"]),
        }
        for row in rows
    ]


def like_ranking(db: sqlite3.Connection, limit: int = 8) -> list[dict]:
    return _rank_rows(db, "COUNT(l.id)", limit)


def view_ranking(db: sqlite3.Connection, limit: int = 8) -> list[dict]:
    return _rank_rows(db, "a.views", limit)


def totals(db: sqlite3.Connection) -> tuple[int, int]:
    base = (
        "FROM assets a WHERE a.deleted_at IS NULL "
        "AND a.status = 'approved' AND a.visibility = 'public'"
    )
    views = db.execute(f"SELECT COALESCE(SUM(a.views), 0) AS n {base}").fetchone()["n"]
    likes = db.execute(
        "SELECT COUNT(*) AS n FROM asset_likes l JOIN assets a ON a.id = l.asset_id "
        "WHERE a.deleted_at IS NULL AND a.status = 'approved' "
        "AND a.visibility = 'public'"
    ).fetchone()["n"]
    return int(likes), int(views)