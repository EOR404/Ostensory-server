from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .config import Config

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(config: Config) -> None:
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(config.db_path)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        # Lightweight in-place migration for databases created before the
        # uploader-IP audit column existed.
        user_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(users)").fetchall()
        }
        if "role" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
            # Databases created before accounts existed only had the operator
            # account, so every existing row must remain an administrator.
            conn.execute("UPDATE users SET role = 'admin'")
        if "display_name" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN display_name TEXT NOT NULL DEFAULT ''")
        if "is_active" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
        if "quota_mb" not in user_columns:
            # Existing accounts keep the historical unlimited behaviour until
            # an administrator sets a quota. New accounts receive the policy
            # value at creation time.
            conn.execute(
                "ALTER TABLE users ADD COLUMN quota_mb INTEGER NOT NULL DEFAULT 0"
            )
        if "oauth_provider" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN oauth_provider TEXT NOT NULL DEFAULT ''")
        if "oauth_subject" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN oauth_subject TEXT NOT NULL DEFAULT ''")
        if "last_login_at" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN last_login_at TEXT")
        # The old schema used 0 for "keep forever".  The new semantics are
        # -1 = keep forever, 0 = disable the recycle bin and delete at once.
        # Convert the old value exactly once so an upgrade cannot silently
        # turn a long-lived recycle bin into immediate permanent deletion.
        trash_marker = conn.execute(
            "SELECT value FROM settings WHERE key = 'trash_semantics_ready'"
        ).fetchone()
        if not trash_marker:
            old_trash = conn.execute(
                "SELECT value FROM settings WHERE key = 'trash_retention_days'"
            ).fetchone()
            if old_trash and str(old_trash["value"]).strip() == "0":
                conn.execute(
                    "UPDATE settings SET value = '-1' WHERE key = 'trash_retention_days'"
                )
            conn.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES('trash_semantics_ready', '2')"
            )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_oauth "
            "ON users(oauth_provider, oauth_subject) "
            "WHERE oauth_provider != '' AND oauth_subject != ''"
        )

        asset_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(assets)").fetchall()
        }
        if "upload_ip" not in asset_columns:
            conn.execute(
                "ALTER TABLE assets ADD COLUMN upload_ip TEXT NOT NULL DEFAULT ''"
            )
        if "box_id" not in asset_columns:
            conn.execute(
                "ALTER TABLE assets ADD COLUMN box_id INTEGER REFERENCES boxes(id) ON DELETE SET NULL"
            )
        if "intro_font_size" not in asset_columns:
            conn.execute(
                "ALTER TABLE assets ADD COLUMN intro_font_size TEXT NOT NULL DEFAULT '15'"
            )
        if "intro_font_style" not in asset_columns:
            conn.execute(
                "ALTER TABLE assets ADD COLUMN intro_font_style TEXT NOT NULL DEFAULT 'italic'"
            )
        if "intro_quote" not in asset_columns:
            conn.execute(
                "ALTER TABLE assets ADD COLUMN intro_quote INTEGER NOT NULL DEFAULT 1"
            )
        if "intro_color" not in asset_columns:
            conn.execute(
                "ALTER TABLE assets ADD COLUMN intro_color TEXT NOT NULL DEFAULT ''"
            )
        if "intro_allow_html" not in asset_columns:
            conn.execute(
                "ALTER TABLE assets ADD COLUMN intro_allow_html INTEGER NOT NULL DEFAULT 0"
            )
        if "owner_id" not in asset_columns:
            conn.execute(
                "ALTER TABLE assets ADD COLUMN owner_id INTEGER REFERENCES users(id) ON DELETE SET NULL"
            )
        if "author_name" not in asset_columns:
            conn.execute(
                "ALTER TABLE assets ADD COLUMN author_name TEXT NOT NULL DEFAULT ''"
            )
        if "status" not in asset_columns:
            conn.execute(
                "ALTER TABLE assets ADD COLUMN status TEXT NOT NULL DEFAULT 'approved'"
            )
        if "visibility" not in asset_columns:
            conn.execute(
                "ALTER TABLE assets ADD COLUMN visibility TEXT NOT NULL DEFAULT 'public'"
            )
        if "views" not in asset_columns:
            conn.execute(
                "ALTER TABLE assets ADD COLUMN views INTEGER NOT NULL DEFAULT 0"
            )
        if "reviewed_at" not in asset_columns:
            conn.execute("ALTER TABLE assets ADD COLUMN reviewed_at TEXT")
        if "review_note" not in asset_columns:
            conn.execute(
                "ALTER TABLE assets ADD COLUMN review_note TEXT NOT NULL DEFAULT ''"
            )
        conn.execute(
            "UPDATE assets SET author_name = '未知用户' "
            "WHERE trim(author_name) = '' AND source = 'guest'"
        )
        conn.execute(
            "UPDATE assets SET author_name = '管理员' "
            "WHERE trim(author_name) = '' AND source != 'guest'"
        )
        admin_row = conn.execute(
            "SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1"
        ).fetchone()
        if admin_row:
            conn.execute(
                "UPDATE assets SET owner_id = ? "
                "WHERE owner_id IS NULL AND source NOT IN ('guest', 'user')",
                (int(admin_row["id"]),),
            )
        album_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(albums)").fetchall()
        }
        if "box_id" not in album_columns:
            conn.execute(
                "ALTER TABLE albums ADD COLUMN box_id INTEGER REFERENCES boxes(id) ON DELETE SET NULL"
            )
        if "guest_quota_mb" not in album_columns:
            conn.execute(
                "ALTER TABLE albums ADD COLUMN guest_quota_mb INTEGER NOT NULL DEFAULT 0"
            )
        box_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(boxes)").fetchall()
        }
        if "access_password_hash" not in box_columns:
            conn.execute("ALTER TABLE boxes ADD COLUMN access_password_hash TEXT")
        blob_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(blobs)").fetchall()
        }
        if "backend_id" not in blob_columns:
            conn.execute(
                "ALTER TABLE blobs ADD COLUMN backend_id INTEGER NOT NULL DEFAULT 1"
            )
        local_backend = conn.execute(
            "SELECT id FROM storage_backends WHERE kind = 'local' ORDER BY id LIMIT 1"
        ).fetchone()
        if not local_backend:
            now = utc_now()
            conn.execute(
                "INSERT INTO storage_backends"
                "(id, kind, name, enabled, priority, quota_mb, threshold_pct, config_json, created_at, updated_at) "
                "VALUES(1, 'local', '本机存储', 1, 10, 0, 90, '{}', ?, ?)",
                (now, now),
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_assets_upload_ip ON assets(upload_ip)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_assets_box ON assets(box_id, deleted_at, id DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_albums_box ON albums(box_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_assets_owner "
            "ON assets(owner_id, status, deleted_at, id DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_assets_status "
            "ON assets(status, deleted_at, id DESC)"
        )
        conn.commit()
    finally:
        conn.close()
    try:
        config.db_path.chmod(0o600)
    except OSError:
        pass


@contextmanager
def db_session(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_db(config: Config):
    """FastAPI dependency. Config is bound by create_app via closure."""
    conn = connect(config.db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def all_settings(conn: sqlite3.Connection) -> dict[str, str]:
    return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM settings")}


def audit(
    conn: sqlite3.Connection,
    action: str,
    actor: str = "",
    target: str = "",
    detail: str = "",
    ip: str = "",
) -> None:
    conn.execute(
        "INSERT INTO audit_log(created_at, actor, action, target, detail, ip) VALUES(?,?,?,?,?,?)",
        (utc_now(), actor, action, target, detail[:2000], ip),
    )
    # Keep the log bounded without expensive maintenance.
    conn.execute(
        "DELETE FROM audit_log WHERE id <= (SELECT MAX(id) - 2000 FROM audit_log)"
    )