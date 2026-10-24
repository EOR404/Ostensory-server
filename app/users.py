from __future__ import annotations

import re
import sqlite3

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{3,64}$")


def user_display_name(user: sqlite3.Row | dict | None) -> str:
    if not user:
        return ""
    keys = user.keys() if hasattr(user, "keys") else user
    display = str(user["display_name"] if "display_name" in keys else "").strip()
    if display:
        return display
    role = str(user["role"] if "role" in keys else "user")
    username = str(user["username"] if "username" in keys else "").strip()
    if role == "admin":
        return "管理员"
    return username or "用户"


def asset_author_name(asset: sqlite3.Row | dict) -> str:
    keys = asset.keys() if hasattr(asset, "keys") else asset

    def value(name: str, default: str = "") -> str:
        return str(asset[name] if name in keys and asset[name] is not None else default)

    owner_id = asset["owner_id"] if "owner_id" in keys else None
    if owner_id:
        joined_display = value("owner_display_name").strip()
        if joined_display:
            return joined_display
        joined_username = value("owner_username").strip()
        if joined_username:
            return "管理员" if value("owner_role") == "admin" else joined_username
    stored = value("author_name").strip()
    if stored:
        return stored
    if value("source") == "guest":
        return "未知用户"
    return "管理员"


def author_user_id(asset: sqlite3.Row | dict) -> int | None:
    keys = asset.keys() if hasattr(asset, "keys") else asset
    if "owner_id" not in keys or asset["owner_id"] is None:
        return None
    try:
        return int(asset["owner_id"])
    except (TypeError, ValueError):
        return None