from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Any

from .access import asset_query
from .database import get_setting, utc_now


def normalized_filename(value: str | None) -> str:
    return (value or "").strip().casefold()


def asset_location(asset: sqlite3.Row | dict) -> str:
    album = str(asset["album_name"] or "").strip() or "未分类"
    box = str(asset["box_name"] or "").strip() or str(asset["album_box_name"] or "").strip()
    return f"{album} / {box}" if box else album


def filename_groups(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return active assets that share a filename, grouped by case-insensitive name."""
    rows = db.execute(
        asset_query()
        + "WHERE a.deleted_at IS NULL "
        "ORDER BY a.original_name COLLATE NOCASE, a.created_at DESC, a.id DESC"
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        key = normalized_filename(row["original_name"])
        if key:
            grouped[key].append(row)
    groups: list[dict[str, Any]] = []
    for key, assets in grouped.items():
        if len(assets) < 2:
            continue
        groups.append(
            {
                "key": key,
                "name": assets[0]["original_name"],
                "assets": assets,
                "count": len(assets),
            }
        )
    groups.sort(key=lambda item: (-item["count"], item["name"].casefold()))
    return groups


def hash_subgroups(assets: list[sqlite3.Row]) -> list[dict[str, Any]]:
    by_hash: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for asset in assets:
        by_hash[str(asset["sha256"] or "")].append(asset)
    groups = [
        {"sha256": sha, "assets": rows, "count": len(rows)}
        for sha, rows in by_hash.items()
        if sha
    ]
    groups.sort(key=lambda item: (-item["count"], item["sha256"]))
    return groups


def hash_summary(assets: list[sqlite3.Row]) -> dict[str, Any]:
    groups = hash_subgroups(assets)
    return {
        "groups": groups,
        "unique": len(groups),
        "confirmed": len(groups) == 1 and len(assets) > 1,
        "different": len(groups) > 1,
    }


def show_all_duplicates(settings: dict[str, Any]) -> bool:
    """Duplicates are only hidden after a check and with the display option unchecked."""
    if str(settings.get("duplicate_check_completed", "0")) != "1":
        return True
    return str(settings.get("show_duplicate_images", "1")) == "1"


def keep_newest_sql(asset_alias: str = "a", blob_alias: str = "b") -> str:
    """SQL predicate keeping the newest asset per (filename, sha256) pair.

    Duplicate detection deliberately starts from filenames, then confirms with
    SHA-256. Two identical files with different names are not treated as a
    duplicate pair by this feature.
    """
    return (
        "NOT EXISTS (SELECT 1 FROM assets dd JOIN blobs dblob ON dblob.id = dd.blob_id "
        "WHERE dd.deleted_at IS NULL "
        f"AND lower(dd.original_name) = lower({asset_alias}.original_name) "
        f"AND dblob.sha256 = {blob_alias}.sha256 "
        f"AND (dd.created_at > {asset_alias}.created_at "
        f"OR (dd.created_at = {asset_alias}.created_at AND dd.id > {asset_alias}.id)))"
    )


def duplicate_filter_enabled(db: sqlite3.Connection) -> bool:
    if get_setting(db, "duplicate_check_completed", "0") != "1":
        return False
    return get_setting(db, "show_duplicate_images", "1") != "1"


def auto_dedupe(db: sqlite3.Connection) -> dict[str, int]:
    """Soft-delete older same-filename/same-hash copies, keeping the newest one."""
    deleted = 0
    kept = 0
    groups = filename_groups(db)
    now = utc_now()
    for group in groups:
        for subgroup in hash_subgroups(group["assets"]):
            rows = subgroup["assets"]
            if len(rows) < 2:
                continue
            rows = sorted(
                rows,
                key=lambda row: (str(row["created_at"] or ""), int(row["id"])),
                reverse=True,
            )
            kept += 1
            for duplicate in rows[1:]:
                db.execute(
                    "UPDATE assets SET deleted_at = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
                    (now, now, duplicate["id"]),
                )
                deleted += 1
    return {"deleted": deleted, "kept_groups": kept}