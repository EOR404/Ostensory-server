from __future__ import annotations

import json
import math
import mimetypes
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from .access import (
    album_chain,
    album_counts,
    album_cover,
    album_effective,
    asset_effective,
    asset_query,
    box_counts,
    box_cover,
    check_album,
    check_asset,
    check_box,
    child_albums,
    ensure_album_path,
)
from .config import Config
from .database import audit, get_setting, set_setting
from .duplicates import duplicate_filter_enabled, keep_newest_sql
from .deps import get_config, get_db_conn, get_geo, get_storage, require_csrf
from .engagement import (
    anonymous_likes_allowed,
    enabled as engagement_enabled,
    like_count,
    like_ranking,
    liked_by,
    record_view,
    set_like,
    totals as engagement_totals,
    view_ranking,
)
from .geo import GeoResolver, client_ip
from .guest_upload import (
    album_allowed,
    album_choices,
    album_quota_mb,
    allowed_album_ids,
    capacity_view,
    cycle_enabled,
    cycle_scope,
    guest_bytes_by_album,
    guest_capacity_state,
    key_required,
    make_room_for_guest_upload,
    normalized_mode,
    parse_titles,
    quota_bytes,
    verify_guest_key,
)
from .quotas import QuotaExceeded, ensure_user_quota, quota_status
from .i18n import (
    LANGUAGE_COOKIE,
    LANGUAGE_COOKIE_MAX_AGE,
    SUPPORTED_LANGUAGES,
    normalize_language,
    request_language,
    tr,
)
from .security import (
    actor_key_for_request,
    cookie_secure,
    current_admin,
    current_user,
    guest_upload_limiter,
    hash_password,
    like_limiter,
    set_unlock_cookie,
    set_visitor_cookie,
    signed_download_url,
    unlock_limiter,
    verify_download_token,
    verify_password,
    verify_view_token,
)
from .services import create_asset_from_upload, extract_workflow, safe_display_name
from .users import user_display_name
from .storage import Storage, StorageError, StorageFull
from .users import asset_author_name, author_user_id
from .web import (
    asset_url,
    flash_redirect,
    media_url,
    parse_int,
    redirect,
    render,
    safe_next,
    site_settings,
)

router = APIRouter()


@router.get("/language/{code}")
def set_language(
    code: str,
    request: Request,
    next: str = "/",
    config: Config = Depends(get_config),
):
    language = normalize_language(code)
    if language not in SUPPORTED_LANGUAGES:
        raise HTTPException(status_code=404)
    destination = safe_next(next, "/")
    response = RedirectResponse(destination, status_code=303)
    response.set_cookie(
        LANGUAGE_COOKIE,
        language,
        max_age=LANGUAGE_COOKIE_MAX_AGE,
        httponly=False,
        samesite="lax",
        secure=cookie_secure(request, config),
        path="/",
    )
    return response


def _admin_logged_in(db: sqlite3.Connection, request: Request) -> bool:
    return bool(current_admin(db, request))


def _external_access_allowed(db: sqlite3.Connection, request: Request) -> bool:
    settings = site_settings(db)
    if settings.get("external_image_enabled") != "1":
        return False
    if settings.get("external_image_guest_enabled", "1") == "1":
        return True
    return current_user(db, request) is not None


def _asset_by_id(db: sqlite3.Connection, asset_id: int) -> sqlite3.Row | None:
    return db.execute(
        asset_query() + "WHERE a.id = ? AND a.deleted_at IS NULL", (asset_id,)
    ).fetchone()


def _album_by_id(db: sqlite3.Connection, album_id: int) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM albums WHERE id = ?", (album_id,)).fetchone()


PUBLIC_ASSET_HIDDEN = {
    "upload_ip",
    "access_password_hash",
    "client_path",
    "region_countries",
    "owner_username",
    "owner_role",
    "owner_display_name",
}

PUBLIC_ASSET_SQL = " AND a.status = 'approved' AND a.visibility = 'public'"


def public_asset_dict(row: sqlite3.Row | dict) -> dict:
    """Strip admin-only fields before a row enters a public template/API."""
    item = dict(row)
    for key in PUBLIC_ASSET_HIDDEN:
        item.pop(key, None)
    return item


def _decorate_assets(
    request: Request,
    db: sqlite3.Connection,
    config: Config,
    geo: GeoResolver,
    rows: list[sqlite3.Row],
    admin: bool,
) -> list[dict]:
    out: list[dict] = []
    for row in rows:
        decision = check_asset(request, db, config, geo, row, admin=admin)
        if not decision.allowed and decision.reason in {"region", "pending", "private"}:
            continue
        item = public_asset_dict(row)
        item["locked"] = decision.locked
        item["nsfw_effective"] = decision.nsfw
        item["author_name"] = asset_author_name(row)
        item["author_id"] = author_user_id(row)
        try:
            item["metadata"] = json.loads(row["metadata_json"] or "{}")
        except ValueError:
            item["metadata"] = {}
        item["lora_count"] = len(item["metadata"].get("loras") or [])
        out.append(item)
    return out


def _decorate_albums(
    request: Request,
    db: sqlite3.Connection,
    config: Config,
    geo: GeoResolver,
    rows: list[sqlite3.Row],
    admin: bool,
) -> list[dict]:
    out: list[dict] = []
    for row in rows:
        decision = check_album(request, db, config, geo, row["id"], admin=admin)
        if not decision.allowed and decision.reason == "region":
            continue
        item = dict(row)
        item["locked"] = decision.locked
        item["nsfw_effective"] = decision.nsfw
        item["asset_count"], _ = album_counts(db, row["id"])
        item["box_name"] = ""
        if row["box_id"]:
            box_row = db.execute(
                "SELECT name FROM boxes WHERE id = ?", (row["box_id"],)
            ).fetchone()
            if box_row:
                item["box_name"] = box_row["name"]
        cover = album_cover(db, row["id"])
        item["cover"] = None
        if cover and not decision.locked:
            cover_decision = check_asset(request, db, config, geo, cover, admin=admin)
            if cover_decision.allowed:
                item["cover"] = dict(cover)
        out.append(item)
    return out


def _decorate_boxes(
    request: Request,
    db: sqlite3.Connection,
    config: Config,
    geo: GeoResolver,
    rows: list[sqlite3.Row],
    admin: bool = False,
) -> list[dict]:
    out: list[dict] = []
    for row in rows:
        item = dict(row)
        item["direct_count"], item["album_count"] = box_counts(db, int(row["id"]))
        decision = check_box(request, db, config, row, admin=admin)
        item["locked"] = decision.locked
        item["has_password"] = bool(row["access_password_hash"])
        item["cover"] = None
        cover = box_cover(db, int(row["id"]))
        if cover:
            decision = check_asset(request, db, config, geo, cover, admin=admin)
            if decision.allowed and not decision.locked:
                item["cover"] = dict(cover)
        out.append(item)
    return out


def _box_by_id(db: sqlite3.Connection, box_id: int) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM boxes WHERE id = ?", (box_id,)).fetchone()


def _id_list(value: str | None, limit: int = 200) -> list[int]:
    out: list[int] = []
    for part in (value or "").replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            number = int(part)
        except ValueError:
            continue
        if number > 0 and number not in out:
            out.append(number)
        if len(out) >= limit:
            break
    return out


def _duplicate_filter(db: sqlite3.Connection) -> str:
    return f" AND {keep_newest_sql()}" if duplicate_filter_enabled(db) else ""


def _assets_by_ids(
    db: sqlite3.Connection, ids: list[int], hide_duplicates: bool = False
) -> list[sqlite3.Row]:
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    duplicate_filter = f" AND {keep_newest_sql()}" if hide_duplicates else ""
    rows = db.execute(
        asset_query()
        + f"WHERE a.deleted_at IS NULL{PUBLIC_ASSET_SQL} AND a.id IN ({placeholders}){duplicate_filter}",
        tuple(ids),
    ).fetchall()
    by_id = {int(row["id"]): row for row in rows}
    return [by_id[item] for item in ids if item in by_id]


def _home_assets(
    db: sqlite3.Connection, settings: dict, per_page: int
) -> list[sqlite3.Row]:
    source = str(settings.get("home_waterfall_source", "latest"))
    hide_duplicates = duplicate_filter_enabled(db)
    if source == "selected":
        rows = _assets_by_ids(
            db, _id_list(settings.get("home_selected_ids")), hide_duplicates
        )
        if rows:
            return rows
    duplicate_filter = f" AND {keep_newest_sql()}" if hide_duplicates else ""
    if source == "all":
        return db.execute(
            asset_query()
            + f"WHERE a.deleted_at IS NULL{PUBLIC_ASSET_SQL}{duplicate_filter} ORDER BY a.id DESC"
        ).fetchall()
    return db.execute(
        asset_query()
        + f"WHERE a.deleted_at IS NULL{PUBLIC_ASSET_SQL}{duplicate_filter} ORDER BY a.id DESC LIMIT ?",
        (per_page * 2,),
    ).fetchall()


def _home_carousel(
    request: Request,
    db: sqlite3.Connection,
    config: Config,
    geo: GeoResolver,
    settings: dict,
    admin: bool,
) -> list[dict]:
    kind = str(settings.get("home_carousel_kind", "assets"))
    ids = _id_list(settings.get("home_carousel_ids"), 30)
    items: list[dict] = []
    if kind == "albums":
        if ids:
            placeholders = ",".join("?" for _ in ids)
            rows = db.execute(
                f"SELECT * FROM albums WHERE id IN ({placeholders}) ORDER BY id DESC",
                tuple(ids),
            ).fetchall()
            order = {item: index for index, item in enumerate(ids)}
            rows = sorted(rows, key=lambda row: order.get(int(row["id"]), 10**9))
        else:
            rows = db.execute(
                "SELECT * FROM albums ORDER BY path COLLATE NOCASE LIMIT 8"
            ).fetchall()
        albums = _decorate_albums(request, db, config, geo, rows, False)
        for album in albums:
            cover = album.get("cover")
            items.append(
                {
                    "kind": "album",
                    "title": album["name"],
                    "subtitle": tr(request, "common.images", count=album["asset_count"]),
                    "url": f"/albums/{album['id']}",
                    "image": media_url(cover["id"], "thumb", cover.get("sha256", "")) if cover else "",
                    "locked": bool(album.get("locked")),
                }
            )
        return items
    hide_duplicates = duplicate_filter_enabled(db)
    if ids:
        rows = _assets_by_ids(db, ids, hide_duplicates)
    else:
        duplicate_filter = f" AND {keep_newest_sql()}" if hide_duplicates else ""
        rows = db.execute(
            asset_query()
            + f"WHERE a.deleted_at IS NULL{PUBLIC_ASSET_SQL}{duplicate_filter} ORDER BY a.id DESC LIMIT 8"
        ).fetchall()
    assets = _decorate_assets(request, db, config, geo, rows, False)
    for asset in assets:
        items.append(
            {
                "kind": "asset",
                "title": asset.get("title") or asset.get("original_name", ""),
                "subtitle": f"{asset.get('width', '')}×{asset.get('height', '')}",
                "url": f"/images/{asset['id']}",
                "image": (
                    ""
                    if asset.get("locked")
                    else media_url(
                        asset["id"], "preview", str(asset.get("sha256") or "")
                    )
                ),
                "locked": bool(asset.get("locked")),
                "nsfw": bool(asset.get("nsfw_effective")),
            }
        )
    return items


def _feed_item(asset: dict, config: Config) -> dict:
    metadata = asset.get("metadata") or {}
    return {
        "id": asset["id"],
        "title": asset.get("title") or asset.get("original_name", ""),
        "url": asset_url(asset["id"], "all"),
        "thumb": media_url(asset["id"], "thumb", asset.get("sha256", "")),
        "download": signed_download_url(asset["id"], config),
        "width": asset.get("width") or 0,
        "height": asset.get("height") or 0,
        "locked": bool(asset.get("locked")),
        "nsfw": bool(asset.get("nsfw_effective")),
        "encrypted": bool(asset.get("encrypted")),
        "kind": metadata.get("kind", ""),
        "lora_count": len(metadata.get("loras") or []),
    }


@router.get("/")
def home(
    request: Request,
    q: str = "",
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    geo: GeoResolver = Depends(get_geo),
):
    admin = _admin_logged_in(db, request)
    settings = site_settings(db)
    albums = _decorate_albums(
        request, db, config, geo, child_albums(db, None), False
    )
    boxes = _decorate_boxes(
        request,
        db,
        config,
        geo,
        db.execute(
            "SELECT * FROM boxes ORDER BY updated_at DESC, id DESC LIMIT 8"
        ).fetchall(),
        False,
    )
    per_page = int(settings["per_page"])
    search = (q or "").strip()[:200]
    if search:
        like = f"%{search}%"
        rows = db.execute(
            asset_query()
            + f"WHERE a.deleted_at IS NULL{PUBLIC_ASSET_SQL}{_duplicate_filter(db)} AND (a.title LIKE ? OR a.original_name LIKE ? "
            "OR a.positive_prompt LIKE ? OR a.negative_prompt LIKE ? OR a.lora_names LIKE ? "
            "OR a.description LIKE ? OR a.metadata_json LIKE ? "
            "OR owner.display_name LIKE ? OR owner.username LIKE ? OR a.author_name LIKE ?) "
            "ORDER BY a.id DESC LIMIT 200",
            (like, like, like, like, like, like, like, like, like, like),
        ).fetchall()
        assets = _decorate_assets(request, db, config, geo, rows, False)
        total = len(assets)
        return render(
            request,
            db,
            "index.html",
            {
                "albums": albums,
                "boxes": boxes,
                "assets": assets[:per_page],
                "search": search,
                "total": total,
                "page_title": tr(request, "home.search_heading", query=search),
                "show_carousel": False,
                "show_waterfall": True,
                "show_albums": False,
                "carousel": [],
            },
        )

    mode = str(settings.get("home_mode", "waterfall"))
    show_carousel = mode in {"carousel", "carousel_waterfall"}
    show_waterfall = mode in {"waterfall", "carousel_waterfall"}
    carousel = (
        _home_carousel(request, db, config, geo, settings, admin)
        if show_carousel
        else []
    )
    if show_carousel and not carousel:
        show_waterfall = True
    assets: list[dict] = []
    if show_waterfall:
        rows = _home_assets(db, settings, per_page)
        assets = _decorate_assets(request, db, config, geo, rows, False)
        if str(settings.get("home_waterfall_source")) != "all":
            assets = assets[:per_page]
    return render(
        request,
        db,
        "index.html",
        {
            "albums": albums,
            "boxes": boxes,
            "assets": assets,
            "search": "",
            "total": None,
            "show_carousel": show_carousel,
            "show_waterfall": show_waterfall,
            "show_albums": show_waterfall,
            "carousel": carousel,
        },
    )


@router.get("/search")
def search_redirect(q: str = ""):
    return RedirectResponse(f"/?q={quote(q)}", status_code=307)


@router.get("/browse")
def browse(
    request: Request,
    page: int = 1,
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    geo: GeoResolver = Depends(get_geo),
):
    admin = _admin_logged_in(db, request)
    settings = site_settings(db)
    per_page = int(settings["per_page"])
    requested_layout = (request.query_params.get("layout") or "").strip().lower()
    browse_layout = (
        requested_layout
        if requested_layout in {"grid", "masonry"}
        else str(settings.get("browse_layout", "masonry"))
    )
    if browse_layout not in {"grid", "masonry"}:
        browse_layout = "masonry"
    page = max(1, page)
    duplicate_filter = _duplicate_filter(db)
    total = int(
        db.execute(
            "SELECT COUNT(*) AS n FROM assets a JOIN blobs b ON b.id = a.blob_id "
            f"WHERE a.deleted_at IS NULL{PUBLIC_ASSET_SQL}{duplicate_filter}"
        ).fetchone()["n"]
    )
    rows = db.execute(
        asset_query()
        + f"WHERE a.deleted_at IS NULL{PUBLIC_ASSET_SQL}{duplicate_filter} ORDER BY a.id DESC LIMIT ? OFFSET ?",
        (per_page, (page - 1) * per_page),
    ).fetchall()
    assets = _decorate_assets(request, db, config, geo, rows, False)
    pages = max(1, math.ceil(total / per_page))
    return render(
        request,
        db,
        "browse.html",
        {
            "assets": assets,
            "page": page,
            "pages": pages,
            "total": total,
            "per_page": per_page,
            "infinite": settings["browse_mode"] == "infinite",
            "browse_layout": browse_layout,
        },
    )


@router.get("/api/feed")
def public_feed(
    request: Request,
    page: int = 1,
    per_page: int = 0,
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    geo: GeoResolver = Depends(get_geo),
):
    settings = site_settings(db)
    page = max(1, page)
    size = per_page if 1 <= per_page <= 60 else int(settings["per_page"])
    size = max(6, min(60, size))
    duplicate_filter = _duplicate_filter(db)
    total = int(
        db.execute(
            "SELECT COUNT(*) AS n FROM assets a JOIN blobs b ON b.id = a.blob_id "
            f"WHERE a.deleted_at IS NULL{PUBLIC_ASSET_SQL}{duplicate_filter}"
        ).fetchone()["n"]
    )
    rows = db.execute(
        asset_query()
        + f"WHERE a.deleted_at IS NULL{PUBLIC_ASSET_SQL}{duplicate_filter} ORDER BY a.id DESC LIMIT ? OFFSET ?",
        (size, (page - 1) * size),
    ).fetchall()
    admin = _admin_logged_in(db, request)
    assets = _decorate_assets(request, db, config, geo, rows, False)
    return JSONResponse(
        {
            "assets": [_feed_item(asset, config) for asset in assets],
            "page": page,
            "pages": max(1, math.ceil(total / size)),
            "total": total,
            "blur_nsfw": bool(settings["blur_nsfw"]),
        }
    )


@router.get("/boxes")
def boxes_index(
    request: Request,
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    geo: GeoResolver = Depends(get_geo),
):
    rows = db.execute("SELECT * FROM boxes ORDER BY name COLLATE NOCASE").fetchall()
    boxes = _decorate_boxes(request, db, config, geo, rows, False)
    return render(request, db, "boxes.html", {"boxes": boxes})


@router.get("/boxes/{box_id}")
def box_detail(
    box_id: int,
    request: Request,
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    geo: GeoResolver = Depends(get_geo),
):
    box = _box_by_id(db, box_id)
    if not box:
        raise HTTPException(status_code=404, detail=tr(request, "error.box_missing"))
    decision = check_box(request, db, config, box, admin=False)
    if decision.locked:
        return render(
            request,
            db,
            "unlock.html",
            {
                "scope": "box",
                "target": dict(box),
                "target_id": box_id,
                "action": f"/unlock/box/{box_id}",
                "next": f"/boxes/{box_id}",
                "nsfw": False,
                "page_title": f"{tr(request, 'unlock.title')} · {box['name']}",
            },
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )
    album_rows = db.execute(
        "SELECT * FROM albums WHERE box_id = ? ORDER BY path COLLATE NOCASE",
        (box_id,),
    ).fetchall()
    albums = _decorate_albums(request, db, config, geo, album_rows, False)
    for item in albums:
        item["box_name"] = box["name"]
    asset_rows = db.execute(
        asset_query()
        + "WHERE a.box_id = ? AND a.deleted_at IS NULL"
        + PUBLIC_ASSET_SQL
        + " ORDER BY a.id DESC",
        (box_id,),
    ).fetchall()
    assets = _decorate_assets(request, db, config, geo, asset_rows, False)
    return render(
        request,
        db,
        "box.html",
        {
            "box": dict(box),
            "albums": albums,
            "assets": assets,
        },
    )


@router.get("/albums")
def albums_index(
    request: Request,
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    geo: GeoResolver = Depends(get_geo),
):
    admin = _admin_logged_in(db, request)
    settings = site_settings(db)
    all_albums = db.execute("SELECT * FROM albums ORDER BY path COLLATE NOCASE").fetchall()
    albums = _decorate_albums(request, db, config, geo, all_albums, False)
    for album in albums:
        album["previews"] = []
        if album["locked"]:
            continue
        rows = db.execute(
            asset_query()
            + "WHERE a.album_id = ? AND a.deleted_at IS NULL"
            + PUBLIC_ASSET_SQL
            + " ORDER BY a.id DESC LIMIT 6",
            (album["id"],),
        ).fetchall()
        album["previews"] = _decorate_assets(
            request, db, config, geo, rows, False
        )[:5]
    layout = str(settings.get("albums_layout", "rows")).lower()
    if layout not in {"rows", "cards", "compact"}:
        layout = "rows"
    return render(
        request,
        db,
        "albums.html",
        {"albums": albums, "albums_layout": layout},
    )


def _public_stats(db: sqlite3.Connection, request: Request) -> dict:
    rows = db.execute(
        "SELECT a.id, a.album_id, a.created_at, a.updated_at, a.metadata_json, "
        "a.positive_prompt, a.lora_names, a.source, b.size "
        "FROM assets a JOIN blobs b ON b.id = a.blob_id "
        "WHERE a.deleted_at IS NULL" + PUBLIC_ASSET_SQL
    ).fetchall()
    album_rows = db.execute(
        "SELECT a.album_id, COUNT(*) AS n FROM assets a "
        "WHERE a.deleted_at IS NULL" + PUBLIC_ASSET_SQL + " AND a.album_id IS NOT NULL "
        "GROUP BY a.album_id ORDER BY n DESC, a.album_id DESC"
    ).fetchall()
    album_names = {
        int(row["id"]): row["name"]
        for row in db.execute("SELECT id, name FROM albums").fetchall()
    }
    storage = int(
        db.execute("SELECT COALESCE(SUM(size), 0) AS n FROM blobs").fetchone()["n"] or 0
    )
    now = datetime.now(timezone.utc)
    recent_7 = recent_30 = 0
    latest = None
    comfy = a1111 = no_meta = with_prompt = 0
    lora_total = 0
    models: set[str] = set()
    base_usage: dict[str, dict[str, object]] = {}
    lora_usage: dict[str, dict[str, object]] = {}
    guest_count = guest_bytes = 0

    def clean_name(value: object) -> str:
        if not isinstance(value, str):
            return ""
        return " ".join(value.replace("\\", "/").split()).strip()

    def add_names(value, bucket: set[str]) -> None:
        if isinstance(value, str):
            if value.strip():
                bucket.add(value.strip())
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    add_names(item, bucket)
                elif isinstance(item, dict):
                    for key in ("name", "model", "model_name", "file"):
                        if item.get(key):
                            add_names(str(item[key]), bucket)
                            break

    def add_usage(bucket: dict[str, dict[str, object]], name: str) -> None:
        key = name.casefold()
        existing = bucket.get(key)
        if existing is None:
            bucket[key] = {"name": name, "count": 1}
        else:
            existing["count"] = int(existing["count"]) + 1

    def first_model(metadata: dict) -> str:
        for field in ("base_model", "checkpoints", "unets"):
            value = metadata.get(field)
            if isinstance(value, str):
                name = clean_name(value)
                if name:
                    return name
            elif isinstance(value, list):
                for item in value:
                    name = clean_name(item)
                    if name:
                        return name
        return ""

    def image_loras(metadata: dict, fallback: str) -> list[str]:
        raw = metadata.get("loras")
        names: list[str] = []
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, str):
                    name = clean_name(item)
                elif isinstance(item, dict):
                    name = clean_name(
                        item.get("name") or item.get("model_name") or item.get("model")
                    )
                else:
                    name = ""
                if name:
                    names.append(name)
        if not names and fallback:
            names = [clean_name(item) for item in str(fallback).split(",")]
        return [name for name in names if name]

    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        kind = str(metadata.get("kind") or "")
        if kind == "comfyui":
            comfy += 1
        elif kind in {"a1111", "forge"}:
            a1111 += 1
        else:
            no_meta += 1
        if row["positive_prompt"] or metadata.get("positive_prompt"):
            with_prompt += 1
        base_name = first_model(metadata)
        if base_name:
            add_usage(base_usage, base_name)
        for key in ("checkpoints", "unets", "vaes"):
            add_names(metadata.get(key), models)
        loras = image_loras(metadata, str(row["lora_names"] or ""))
        seen_loras: set[str] = set()
        for name in loras:
            key = name.casefold()
            if key in seen_loras:
                continue
            seen_loras.add(key)
            add_usage(lora_usage, name)
        lora_total += len(seen_loras)
        if row["source"] == "guest":
            guest_count += 1
            guest_bytes += int(row["size"] or 0)
        created = _parse_stat_time(row["created_at"])
        if created:
            age = now - created
            if age.total_seconds() >= 0:
                if age.days < 7:
                    recent_7 += 1
                if age.days < 30:
                    recent_30 += 1
            latest = created if latest is None or created > latest else latest

    albums = [
        {"name": album_names.get(int(row["album_id"]), tr(request, "common.unnamed_album")), "count": int(row["n"])}
        for row in album_rows[:8]
    ]

    def usage_rows(bucket: dict[str, dict[str, object]]) -> list[dict[str, object]]:
        return sorted(
            (
                {"name": str(item["name"]), "count": int(item["count"])}
                for item in bucket.values()
            ),
            key=lambda item: (-int(item["count"]), str(item["name"]).casefold()),
        )

    base_model_usage = usage_rows(base_usage)
    lora_usage_rows = usage_rows(lora_usage)
    likes_total, views_total = engagement_totals(db)
    return {
        "images": len(rows),
        "albums": len(album_names),
        "storage": storage,
        "asset_bytes": sum(int(row["size"] or 0) for row in rows),
        "recent_7": recent_7,
        "recent_30": recent_30,
        "latest": latest,
        "comfy": comfy,
        "a1111": a1111,
        "no_meta": no_meta,
        "with_prompt": with_prompt,
        "lora_total": lora_total,
        "lora_kinds": len(lora_usage_rows),
        "models": len(models),
        "base_models": len(base_usage),
        "base_model_usage": base_model_usage,
        "lora_usage": lora_usage_rows,
        "base_model_usage_max": int(base_model_usage[0]["count"]) if base_model_usage else 0,
        "lora_usage_max": int(lora_usage_rows[0]["count"]) if lora_usage_rows else 0,
        "like_total": likes_total,
        "view_total": views_total,
        "top_liked": like_ranking(db, 8),
        "top_viewed": view_ranking(db, 8),
        "top_albums": albums,
        "guest_count": guest_count,
        "guest_bytes": guest_bytes,
        "updated_at": now,
    }


def _parse_stat_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@router.get("/stats")
def stats_page(
    request: Request,
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    geo: GeoResolver = Depends(get_geo),
):
    settings = site_settings(db)
    if str(settings.get("stats_enabled", "1")) != "1":
        raise HTTPException(status_code=404, detail=tr(request, "error.stats_disabled"))
    show = {
        "counts": str(settings.get("stats_show_counts", "1")) == "1",
        "storage": str(settings.get("stats_show_storage", "1")) == "1",
        "metadata": str(settings.get("stats_show_metadata", "1")) == "1",
        "activity": str(settings.get("stats_show_activity", "1")) == "1",
        "albums": str(settings.get("stats_show_albums", "1")) == "1",
        "guest": str(settings.get("stats_show_guest", "0")) == "1",
        "engagement": str(settings.get("stats_show_engagement", "1")) == "1",
        "like_rank": str(settings.get("stats_show_like_rank", "1")) == "1",
        "view_rank": str(settings.get("stats_show_view_rank", "1")) == "1",
    }
    return render(
        request,
        db,
        "stats.html",
        {
            "stats": _public_stats(db, request),
            "stats_show": show,
            "page_title": tr(request, "stats.title"),
        },
    )


def _guest_context(
    request: Request,
    db: sqlite3.Connection,
    config: Config,
    geo: GeoResolver,
) -> dict:
    settings = site_settings(db)
    mode = normalized_mode(db)
    choices = album_choices(db)
    allowed = allowed_album_ids(db)
    if allowed:
        choices = [item for item in choices if int(item["id"]) in allowed]
    quota_mb = parse_int(settings.get("guest_upload_quota_mb"), 1024, 0, 1024 * 1024)
    used_bytes = quota_bytes(db)
    total_capacity = capacity_view(quota_mb, used_bytes)
    usage_by_album = guest_bytes_by_album(db)
    for item in choices:
        album_usage = usage_by_album.get(int(item["id"]), 0)
        capacity = capacity_view(album_quota_mb(item), album_usage)
        item["guest_limit_mb"] = capacity["limit_mb"]
        item["guest_used_mb"] = capacity["used_mb"]
        item["guest_remaining_mb"] = capacity["remaining_mb"]
        item["guest_percent"] = capacity["percent"]
        item["guest_unlimited"] = capacity["unlimited"]
        item["guest_full"] = capacity["full"]
    max_file_mb = min(
        parse_int(settings.get("guest_upload_max_mb"), 20, 1, 2048),
        parse_int(settings.get("max_upload_mb"), 200, 1, 2048),
    )
    return {
        "guest_albums": choices,
        "guest_default_album_id": choices[0]["id"] if choices else None,
        "upload_as_user": False,
        "upload_user": None,
        "upload_require_review": settings.get("upload_require_review") == "1",
        "guest_key_mode": mode,
        "guest_key_required": key_required(mode),
        "guest_key_configured": bool(
            get_setting(db, "guest_upload_key_hash", "")
            if mode == "fixed"
            else get_setting(db, "guest_upload_once_hash", "")
            if mode == "once"
            else True
        ),
        "guest_max_file_mb": max_file_mb,
        "guest_quota_mb": quota_mb,
        "guest_quota_used_mb": total_capacity["used_mb"],
        "guest_quota_remaining_mb": total_capacity["remaining_mb"],
        "guest_quota_percent": total_capacity["percent"],
        "guest_quota_full": total_capacity["full"],
        "guest_total_capacity": total_capacity,
        "guest_cycle_enabled": cycle_enabled(db),
        "guest_cycle_scope": cycle_scope(db),
        "guest_interval_seconds": parse_int(
            settings.get("guest_upload_interval_seconds"), 30, 0, 86400
        ),
        "guest_max_files": parse_int(
            settings.get("guest_upload_max_files"), 20, 1, 100
        ),
    }


def _upload_context(
    request: Request,
    db: sqlite3.Connection,
    config: Config,
    geo: GeoResolver,
) -> dict:
    settings = site_settings(db)
    viewer = current_user(db, request)
    is_admin = bool(viewer and viewer["role"] == "admin")
    guest_can_upload = settings.get("guest_upload_enabled") == "1"
    user_switch_on = settings.get("user_upload_enabled") == "1"
    # If guest upload is enabled while user upload is off, an administrator can
    # still use the public guest flow (the historical behaviour). If both
    # switches are off, the administrator keeps a personal upload page instead
    # of being locked out; normal accounts still receive 403.
    admin_guest_mode = is_admin and not user_switch_on and guest_can_upload
    user_can_upload = bool(
        viewer
        and (user_switch_on or (is_admin and not admin_guest_mode))
    )
    if not user_can_upload and not guest_can_upload:
        raise HTTPException(status_code=404, detail=tr(request, "error.guest_disabled"))
    if viewer and not user_can_upload and not admin_guest_mode:
        raise HTTPException(status_code=403, detail="当前账号没有上传权限。")
    context = _guest_context(request, db, config, geo)
    if user_can_upload:
        albums = album_choices(db)
        if not albums:
            default_path = get_setting(db, "default_album_path", "未分类") or "未分类"
            ensure_album_path(db, default_path, create=True)
            db.commit()
            albums = album_choices(db)
        for album in albums:
            decision = check_album(
                request, db, config, geo, int(album["id"]), admin=False
            )
            album["upload_locked"] = bool(decision.locked)
            album["upload_region_blocked"] = bool(
                not decision.allowed and decision.reason == "region"
            )
        context.update(
            {
                "guest_albums": albums,
                "guest_default_album_id": albums[0]["id"] if albums else None,
                "upload_as_user": True,
                "upload_user": viewer,
                "upload_require_review": settings.get("upload_require_review") == "1",
                "user_quota": quota_status(db, int(viewer["id"])),
                "guest_key_required": False,
                "guest_max_file_mb": min(
                    parse_int(settings.get("max_upload_mb"), 200, 1, 2048),
                    2048,
                ),
            }
        )
    return context


@router.get("/upload")
def guest_upload_page(
    request: Request,
    album_id: str = "",
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    geo: GeoResolver = Depends(get_geo),
):
    context = _upload_context(request, db, config, geo)
    if album_id.strip().isdigit():
        requested = int(album_id)
        if any(int(item["id"]) == requested for item in context["guest_albums"]):
            context["guest_default_album_id"] = requested
    return render(request, db, "guest_upload.html", context)


def _guest_capacity_error(
    request: Request,
    album_name: str,
    state: dict,
    *,
    cycle_on: bool,
    cleanup_happened: bool,
) -> str:
    if state["album_short"] and state["total_short"]:
        message = tr(request, "error.upload_capacity_both", album=album_name)
    elif state["album_short"]:
        message = tr(request, "error.upload_album_quota", album=album_name)
    else:
        message = tr(request, "error.upload_quota")
    if cleanup_happened:
        message += tr(request, "error.upload_capacity_no_cleanup")
    elif cycle_on:
        message += tr(request, "error.upload_capacity_no_candidates")
    return message


def _guest_upload_size(upload: UploadFile) -> int:
    if upload.size is not None:
        return int(upload.size)
    stream = upload.file
    try:
        position = stream.tell()
        stream.seek(0, 2)
        size = int(stream.tell())
        stream.seek(position)
        return size
    except (OSError, ValueError):
        return 0


@router.post("/upload")
async def guest_upload_submit(
    request: Request,
    files: list[UploadFile] | None = File(None),
    album_id: str = Form(""),
    key: str = Form(""),
    title: str = Form(""),
    description: str = Form(""),
    titles: str = Form("[]"),
    access_password: str = Form(""),
    visibility: str = Form("public"),
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    geo: GeoResolver = Depends(get_geo),
    storage: Storage = Depends(get_storage),
):
    require_csrf(request, csrf, config)
    settings = site_settings(db)
    viewer = current_user(db, request)
    is_admin = bool(viewer and viewer["role"] == "admin")
    guest_upload = settings.get("guest_upload_enabled") == "1"
    user_switch_on = settings.get("user_upload_enabled") == "1"
    admin_guest_upload = bool(is_admin and not user_switch_on and guest_upload)
    user_upload = bool(
        viewer and (user_switch_on or (is_admin and not admin_guest_upload))
    )
    if not viewer and not guest_upload:
        raise HTTPException(status_code=404, detail=tr(request, "error.guest_disabled"))
    if viewer and not user_upload and not admin_guest_upload:
        raise HTTPException(status_code=403, detail="当前账号没有上传权限。")
    uploads = [item for item in (files or []) if item.filename]
    context = _upload_context(request, db, config, geo)
    require_review = settings.get("upload_require_review") == "1"
    access_password = access_password.strip()[:200]
    visibility_value = visibility if visibility in {"public", "private"} else "public"
    if not user_upload:
        visibility_value = "public"
    if not uploads:
        return redirect("/upload?error=" + quote(tr(request, "error.upload_pick_one")))
    if len(uploads) > context["guest_max_files"]:
        return redirect(
            "/upload?error="
            + quote(tr(request, "error.upload_too_many", count=context["guest_max_files"]))
        )

    # Resolve the destination before spending a one-time key.
    target_album = None
    if album_id.strip().isdigit():
        target_album = db.execute(
            "SELECT * FROM albums WHERE id = ?", (int(album_id),)
        ).fetchone()
    else:
        default_path = get_setting(db, "default_album_path", "未分类") or "未分类"
        target_album = ensure_album_path(db, default_path, create=bool(user_upload))
    if not target_album:
        return redirect("/upload?error=" + quote(tr(request, "error.album_missing")))
    if not user_upload and not album_allowed(db, int(target_album["id"])):
        return redirect("/upload?error=" + quote(tr(request, "error.album_guest_blocked")))
    album_decision = check_album(
        request, db, config, geo, int(target_album["id"]), admin=False
    )
    if album_decision.locked:
        next_url = f"/upload?album_id={int(target_album['id'])}"
        return redirect(
            f"/unlock/album/{int(target_album['id'])}?next={quote(next_url)}"
        )
    if not album_decision.allowed:
        return redirect("/upload?error=" + quote(tr(request, "error.album_guest_region")))

    max_file_bytes = context["guest_max_file_mb"] * 1024 * 1024
    sizes = [_guest_upload_size(item) for item in uploads]
    if any(size > max_file_bytes for size in sizes):
        return redirect(
            "/upload?error="
            + quote(tr(request, "error.upload_too_large", mb=context["guest_max_file_mb"]))
        )
    ip = client_ip(request, config)
    # Guest uploads are only linked to an IP when the administrator explicitly
    # enables the privacy-sensitive trace option.
    trace_ip = (not user_upload) and settings.get("guest_upload_log_ip") == "1"
    mode = context["guest_key_mode"]
    if user_upload:
        if not guest_upload_limiter.allow(f"user-up:{int(viewer['id'])}", 12, 60):
            return redirect("/upload?error=" + quote(tr(request, "error.upload_rate", seconds=60)))
    else:
        interval = context["guest_interval_seconds"]
        if interval and not guest_upload_limiter.allow(f"guest-up:{ip}", 1, interval):
            return redirect(
                "/upload?error="
                + quote(tr(request, "error.upload_rate", seconds=interval))
            )
        if not guest_upload_limiter.allow(f"guest-key:{ip}", 12, 300):
            return redirect("/upload?error=" + quote(tr(request, "error.upload_attempts")))
        ok, key_error = verify_guest_key(db, config, mode, key, consume=False)
        if not ok:
            key_error_keys = {
                "请填写访客上传密钥。": "error.guest_key_missing",
                "管理员还没有设置固定密钥。": "error.guest_key_not_set",
                "上传密钥不正确。": "error.guest_key_wrong",
                "上传密钥不正确或已经过期。": "error.guest_key_expired",
                "一次性密钥已经被使用，请联系管理员重新生成。": "error.guest_key_used",
                "管理员还没有生成一次性密钥。": "error.guest_key_not_generated",
                "访客上传密钥配置不正确。": "error.guest_key_config",
            }
            return redirect(
                "/upload?error="
                + quote(tr(request, key_error_keys.get(key_error, "error.guest_key")))
            )

    access_hash = hash_password(access_password) if access_password else None
    selected_titles = parse_titles(titles)
    created = 0
    duplicates = 0
    errors: list[str] = []
    deleted_assets: list[dict] = []
    new_asset_ids: set[int] = set()
    quota_pending = 0
    for index, upload in enumerate(uploads):
        incoming = sizes[index] or 0
        if user_upload and viewer:
            try:
                ensure_user_quota(
                    db,
                    storage,
                    int(viewer["id"]),
                    incoming,
                    pending_bytes=quota_pending,
                )
            except QuotaExceeded as exc:
                errors.append(f"{safe_display_name(upload.filename or '')}: {exc}")
                continue
        if not user_upload:
            state = guest_capacity_state(
                db,
                int(target_album["id"]),
                incoming,
                context["guest_quota_mb"],
            )
            if state["album_short"] or state["total_short"]:
                removed: list[dict] = []
                if context["guest_cycle_enabled"]:
                    removed = make_room_for_guest_upload(
                        db,
                        storage,
                        int(target_album["id"]),
                        incoming,
                        context["guest_quota_mb"],
                        context["guest_cycle_scope"],
                        exclude_ids=new_asset_ids,
                    )
                    deleted_assets.extend(removed)
                    state = guest_capacity_state(
                        db,
                        int(target_album["id"]),
                        incoming,
                        context["guest_quota_mb"],
                    )
                if state["album_short"] or state["total_short"]:
                    errors.append(
                        _guest_capacity_error(
                            request,
                            str(target_album["name"]),
                            state,
                            cycle_on=context["guest_cycle_enabled"],
                            cleanup_happened=bool(removed),
                        )
                    )
                    continue
        chosen_title = (
            selected_titles[index]
            if index < len(selected_titles) and selected_titles[index]
            else title.strip()
        )
        try:
            upload.file.seek(0)
            asset, status = create_asset_from_upload(
                db,
                storage,
                upload.file,
                upload.filename or "upload",
                int(target_album["id"]),
                source_name="user" if user_upload else "guest",
                upload_ip=ip if trace_ip else "",
                title=chosen_title,
                description=description,
                owner_id=(
                    int(viewer["id"])
                    if viewer and (user_upload or admin_guest_upload)
                    else None
                ),
                author_name=(
                    user_display_name(viewer)
                    if viewer and (user_upload or admin_guest_upload)
                    else "未知用户"
                ),
                status="pending" if require_review else "approved",
                visibility=visibility_value,
                access_password_hash=access_hash,
            )
            if asset is not None:
                new_asset_ids.add(int(asset["id"]))
            created += 1
            if status == "duplicate":
                duplicates += 1
            else:
                quota_pending += incoming
        except StorageFull as exc:
            errors.append(f"{safe_display_name(upload.filename or '')}: {exc}")
        except (StorageError, ValueError, OSError) as exc:
            errors.append(f"{safe_display_name(upload.filename or '')}: {exc}")
    if created and mode == "once" and not user_upload:
        set_setting(db, "guest_upload_once_used", "1")
    if deleted_assets:
        audit(
            db,
            "guest.auto_delete",
            actor="guest",
            target=str(target_album["id"]),
            detail=(
                f"deleted={len(deleted_assets)} "
                f"scope={context['guest_cycle_scope']} "
                f"ids={','.join(str(item['id']) for item in deleted_assets[:30])}"
            ),
            ip=ip if trace_ip else "",
        )
    audit(
        db,
        "user.upload" if user_upload else "guest.upload",
        actor=str(viewer["username"]) if user_upload and viewer else "guest",
        target=str(target_album["id"]),
        detail=(
            f"created={created} errors={len(errors)} "
            f"auto_deleted={len(deleted_assets)}"
        ),
        ip=ip if trace_ip else "",
    )
    db.commit()
    cleanup_message = (
        tr(request, "guest.cycle_deleted", count=len(deleted_assets))
        if deleted_assets
        else ""
    )
    if not created:
        message = "；".join(errors[:3]) or tr(request, "error.upload_none")
        return redirect("/upload?error=" + quote(message + cleanup_message))
    message = tr(
        request,
        "guest.uploaded",
        count=created,
        album=target_album["name"],
    )
    if require_review:
        message += " 图片已提交审核，管理员放行后才会公开显示。"
    if access_password:
        message += " 已设置访问密码，查看详情或原图时需要先输入该密码。"
    if duplicates:
        message += tr(request, "guest.duplicates", count=duplicates)
    if errors:
        message += tr(
            request, "guest.partial", message="；".join(errors[:3])
        )
    if cleanup_message:
        message += cleanup_message
    return flash_redirect("/upload", message)


@router.get("/albums/{album_id}")
def album_detail(
    album_id: int,
    request: Request,
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    geo: GeoResolver = Depends(get_geo),
):
    album = _album_by_id(db, album_id)
    if not album:
        raise HTTPException(status_code=404, detail=tr(request, "error.album_missing"))
    admin = _admin_logged_in(db, request)
    # Public pages always honor the visitor password gate.  The admin session
    # only bypasses media thumbnails inside the admin console; otherwise an
    # operator previewing the public site would never see the unlock dialog.
    decision = check_album(request, db, config, geo, album_id, admin=False)
    if not decision.allowed and decision.reason == "region":
        raise HTTPException(status_code=451, detail=tr(request, "error.region_short"))
    chain = album_chain(db, album_id)
    children = _decorate_albums(
        request, db, config, geo, child_albums(db, album_id), False
    )
    if decision.locked:
        return render(
            request,
            db,
            "unlock.html",
            {
                "scope": "album",
                "target": dict(album),
                "target_id": album_id,
                "action": f"/unlock/album/{album_id}",
                "next": f"/albums/{album_id}",
                "nsfw": decision.nsfw,
                "page_title": f"{tr(request, 'unlock.title')} · {album['name']}",
            },
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )
    rows = db.execute(
        asset_query()
        + "WHERE a.album_id = ? AND a.deleted_at IS NULL"
        + PUBLIC_ASSET_SQL
        + " ORDER BY a.id DESC",
        (album_id,),
    ).fetchall()
    assets = _decorate_assets(request, db, config, geo, rows, False)
    box_row = _box_by_id(db, int(album["box_id"])) if album["box_id"] else None
    return render(
        request,
        db,
        "album.html",
        {
            "album": dict(album),
            "box": dict(box_row) if box_row else None,
            "breadcrumb": list(reversed(chain)),
            "children": children,
            "assets": assets,
            "nsfw": decision.nsfw,
        },
    )


@router.get("/images/{asset_id}")
def image_detail(
    asset_id: int,
    request: Request,
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    geo: GeoResolver = Depends(get_geo),
    storage: Storage = Depends(get_storage),
):
    asset = _asset_by_id(db, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail=tr(request, "error.image_missing"))
    admin = _admin_logged_in(db, request)
    requested_source = (request.query_params.get("from") or "").strip().lower()
    gallery_source = (
        "album" if requested_source == "album" and asset["album_id"] else "all"
    )
    detail_next = f"/images/{asset_id}" + (
        "?from=album" if gallery_source == "album" else ""
    )
    decision = check_asset(request, db, config, geo, asset, admin=False)
    if not decision.allowed and decision.reason == "region":
        raise HTTPException(status_code=451, detail=tr(request, "error.region_short"))
    if not decision.allowed and decision.reason in {"pending", "private"}:
        raise HTTPException(status_code=404, detail=tr(request, "error.image_missing"))
    if decision.locked:
        return render(
            request,
            db,
            "unlock.html",
            {
                "scope": "asset",
                "target": dict(asset),
                "target_id": asset_id,
                "action": f"/unlock/asset/{asset_id}",
                "next": detail_next,
                "nsfw": decision.nsfw,
                "page_title": tr(request, "unlock.title"),
            },
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )
    try:
        metadata = json.loads(asset["metadata_json"] or "{}")
    except ValueError:
        metadata = {}
    workflow_json = (metadata.get("raw") or {}).get("workflow")
    if workflow_json is None and metadata.get("client_parsed"):
        # Client syncs send normalized metadata only; recover the workflow from
        # the original file lazily so workflow.json download still works.
        workflow_json = extract_workflow(db, storage, asset)
    album_row = _album_by_id(db, asset["album_id"]) if asset["album_id"] else None
    box_id = asset["box_id"] or (album_row["box_id"] if album_row else None)
    box_row = _box_by_id(db, int(box_id)) if box_id else None
    # Keep navigation in the exact order shown by the album / all-images page.
    # Both pages list newest first, so "previous" is the next higher id.
    if gallery_source == "album":
        scope_sql = "a.album_id = ? AND a.status = 'approved' AND a.visibility = 'public' AND "
        scope_params: tuple = (asset["album_id"],)
    else:
        scope_sql = "a.status = 'approved' AND a.visibility = 'public' AND "
        scope_params = ()
    prev_row = db.execute(
        asset_query()
        + f"WHERE {scope_sql}a.id > ? AND a.deleted_at IS NULL ORDER BY a.id ASC LIMIT 1",
        (*scope_params, asset_id),
    ).fetchone()
    next_row = db.execute(
        asset_query()
        + f"WHERE {scope_sql}a.id < ? AND a.deleted_at IS NULL ORDER BY a.id DESC LIMIT 1",
        (*scope_params, asset_id),
    ).fetchone()
    total_row = db.execute(
        "SELECT COUNT(*) AS n FROM assets a WHERE " + (scope_sql or "1=1 AND ") + "a.deleted_at IS NULL",
        scope_params,
    ).fetchone()
    position_row = db.execute(
        "SELECT COUNT(*) AS n FROM assets a WHERE "
        + (scope_sql or "1=1 AND ")
        + "a.id >= ? AND a.deleted_at IS NULL",
        (*scope_params, asset_id),
    ).fetchone()
    related_rows = []
    if asset["album_id"]:
        related_rows = db.execute(
            asset_query()
            + "WHERE a.album_id = ? AND a.id != ? AND a.deleted_at IS NULL"
            + PUBLIC_ASSET_SQL
            + " ORDER BY a.id DESC LIMIT 10",
            (asset["album_id"], asset_id),
        ).fetchall()
    related = _decorate_assets(request, db, config, geo, related_rows, False)[:8]
    effective = asset_effective(db, asset)
    viewer = current_user(db, request)
    actor_key, visitor_cookie = actor_key_for_request(request, db, config, viewer)
    engage_on = engagement_enabled(db)
    view_added = record_view(db, asset_id, actor_key) if engage_on else False
    if engage_on:
        db.commit()
    likes = like_count(db, asset_id) if engage_on else 0
    views = (int(asset["views"] or 0) + (1 if view_added else 0)) if engage_on else 0
    liked = liked_by(db, asset_id, actor_key) if engage_on else False
    can_like = engage_on and bool(viewer or anonymous_likes_allowed(db))
    asset_item = public_asset_dict(asset)
    asset_item["author_name"] = asset_author_name(asset)
    asset_item["author_id"] = author_user_id(asset)
    response = render(
        request,
        db,
        "image.html",
        {
            "asset": asset_item,
            "metadata": metadata,
            "engagement_enabled": engage_on,
            "like_count": likes,
            "view_count": views,
            "liked": liked,
            "can_like": can_like,
            "album": dict(album_row) if album_row else None,
            "box": dict(box_row) if box_row else None,
            "related": related,
            "gallery_source": gallery_source,
            "prev_asset": dict(prev_row) if prev_row else None,
            "next_asset": dict(next_row) if next_row else None,
            "detail_position": int(position_row["n"]) if position_row else 1,
            "detail_total": int(total_row["n"]) if total_row else 1,
            "nsfw": decision.nsfw,
            "locked": False,
            "region_policy": effective.region_mode,
            "workflow_json": workflow_json,
            "prompt_json": (metadata.get("raw") or {}).get("prompt"),
        },
    )
    set_visitor_cookie(response, request, config, visitor_cookie)
    return response


@router.post("/images/{asset_id}/like")
def image_like(
    asset_id: int,
    request: Request,
    next: str = Form(""),
    desired: str = Form(""),
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    geo: GeoResolver = Depends(get_geo),
):
    wants_json = (
        request.headers.get("x-requested-with", "").lower() == "fetch"
        or "application/json" in request.headers.get("accept", "").lower()
    )
    require_csrf(request, csrf, config)
    if not engagement_enabled(db):
        raise HTTPException(status_code=404, detail="点赞功能未开启")
    asset = _asset_by_id(db, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail=tr(request, "error.image_missing"))
    decision = check_asset(
        request, db, config, geo, asset, admin=_admin_logged_in(db, request)
    )
    if not decision.allowed:
        raise HTTPException(status_code=404, detail=tr(request, "error.image_missing"))
    viewer = current_user(db, request)
    if not viewer and not anonymous_likes_allowed(db):
        destination = safe_next(next, f"/images/{asset_id}")
        return redirect("/login?next=" + quote(destination))
    ip = client_ip(request, config)
    actor_key, visitor_cookie = actor_key_for_request(request, db, config, viewer)
    limited = not like_limiter.allow(f"like:{ip}:{asset_id}", 20, 60)
    if not limited:
        limited = not like_limiter.allow(f"like-actor:{actor_key}", 30, 60)
    if limited:
        if wants_json:
            response = JSONResponse(
                {"ok": False, "error": "操作过于频繁，请稍后再试。"},
                status_code=429,
                headers={"Cache-Control": "no-store"},
            )
        else:
            response = redirect(
                safe_next(next, f"/images/{asset_id}")
                + "?msg="
                + quote("操作过于频繁，请稍后再试。")
            )
        set_visitor_cookie(response, request, config, visitor_cookie)
        return response
    desired_state = True if desired == "1" else False if desired == "0" else None
    liked, count, changed = set_like(
        db,
        asset_id,
        actor_key,
        int(viewer["id"]) if viewer else None,
        desired_state,
    )
    if changed:
        audit(
            db,
            "asset.like" if liked else "asset.unlike",
            actor=str(viewer["username"]) if viewer else "guest",
            target=str(asset_id),
            ip=ip if viewer else "",
        )
    db.commit()
    if wants_json:
        response = JSONResponse(
            {"ok": True, "liked": liked, "count": count, "changed": changed},
            headers={"Cache-Control": "no-store"},
        )
        set_visitor_cookie(response, request, config, visitor_cookie)
        return response
    destination = safe_next(next, f"/images/{asset_id}")
    separator = "&" if "?" in destination else "?"
    response = redirect(
        f"{destination}{separator}msg="
        + quote("已点赞。" if liked else "已取消点赞。")
        + "#engagement"
    )
    set_visitor_cookie(response, request, config, visitor_cookie)
    return response


def _unlock_target(
    scope: str,
    target_id: int,
    db: sqlite3.Connection,
) -> tuple[sqlite3.Row | None, str | None]:
    if scope == "album":
        row = db.execute("SELECT * FROM albums WHERE id = ?", (target_id,)).fetchone()
        return row, album_effective(db, target_id).password_hash
    if scope == "box":
        row = _box_by_id(db, target_id)
        return row, (row["access_password_hash"] if row else None)
    row = _asset_by_id(db, target_id)
    return row, asset_effective(db, row).password_hash if row else None


@router.get("/unlock/{scope}/{target_id}")
def unlock_form(
    scope: str,
    target_id: int,
    request: Request,
    next: str = "",
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    geo: GeoResolver = Depends(get_geo),
):
    if scope not in {"album", "asset", "box"}:
        raise HTTPException(status_code=404)
    target, password_hash = _unlock_target(scope, target_id, db)
    if not target:
        raise HTTPException(status_code=404, detail=tr(request, "error.target_missing"))
    if scope == "album":
        decision = check_album(request, db, config, geo, target_id, admin=False)
    elif scope == "box":
        decision = check_box(request, db, config, target, admin=False)
    else:
        decision = check_asset(request, db, config, geo, target, admin=False)
    if decision.reason == "region":
        raise HTTPException(status_code=451, detail=tr(request, "error.region_short"))
    if not password_hash:
        fallback = f"/boxes/{target_id}" if scope == "box" else f"/{scope}s/{target_id}"
        return redirect(safe_next(next, fallback))
    return render(
        request,
        db,
        "unlock.html",
        {
            "scope": scope,
            "target": dict(target),
            "target_id": target_id,
            "action": f"/unlock/{scope}/{target_id}",
            "next": safe_next(
                next,
                f"/{'albums' if scope == 'album' else 'boxes' if scope == 'box' else 'images'}/{target_id}",
            ),
            "nsfw": decision.nsfw,
            "page_title": tr(request, "unlock.page_title"),
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/unlock/{scope}/{target_id}")
def unlock_submit(
    scope: str,
    target_id: int,
    request: Request,
    password: str = Form(""),
    csrf: str = Form(""),
    next: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    geo: GeoResolver = Depends(get_geo),
):
    if scope not in {"album", "asset", "box"}:
        raise HTTPException(status_code=404)
    require_csrf(request, csrf, config)
    target, password_hash = _unlock_target(scope, target_id, db)
    if not target:
        raise HTTPException(status_code=404, detail=tr(request, "error.target_missing"))
    ip = getattr(request.state, "client_ip", "") or (request.client.host if request.client else "")
    if not unlock_limiter.allow(f"unlock:{ip}:{scope}:{target_id}", 5, 60):
        raise HTTPException(status_code=429, detail=tr(request, "error.unlock_rate"))
    fallback = f"/boxes/{target_id}" if scope == "box" else f"/{'albums' if scope == 'album' else 'images'}/{target_id}"
    destination = safe_next(next, fallback)
    if password_hash and verify_password(password, password_hash):
        response = RedirectResponse(destination, status_code=303)
        if scope == "album":
            effective = album_effective(db, target_id)
            cookie_scope = effective.password_scope or f"album:{target_id}"
        elif scope == "box":
            cookie_scope = f"box:{target_id}"
        else:
            cookie_scope = f"asset:{target_id}"
            effective = asset_effective(db, target)
            if not target["access_password_hash"] and effective.password_scope:
                # An asset protected by its album/box must unlock that outer
                # scope, otherwise the signed cookie would never satisfy check_asset().
                cookie_scope = effective.password_scope
        set_unlock_cookie(response, request, config, add_scope=cookie_scope)
        return response
    if scope == "album":
        decision = check_album(request, db, config, geo, target_id)
    elif scope == "box":
        decision = check_box(request, db, config, target)
    else:
        decision = check_asset(request, db, config, geo, target)
    return render(
        request,
        db,
        "unlock.html",
        {
            "scope": scope,
            "target": dict(target),
            "target_id": target_id,
            "action": f"/unlock/{scope}/{target_id}",
            "next": destination,
            "nsfw": decision.nsfw,
            "unlock_error": tr(request, "error.unlock_wrong"),
            "page_title": tr(request, "unlock.page_title"),
        },
        status_code=200,
        headers={"Cache-Control": "no-store"},
    )


def _media_response(
    asset_id: int,
    kind: str,
    request: Request,
    db: sqlite3.Connection,
    config: Config,
    geo: GeoResolver,
    storage: Storage,
    download: bool = False,
    allow_original: bool = False,
):
    asset = _asset_by_id(db, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail=tr(request, "error.image_missing"))
    admin = _admin_logged_in(db, request)
    if kind == "original" and not admin and not allow_original:
        if not _external_access_allowed(db, request):
            raise HTTPException(status_code=403, detail=tr(request, "error.external_disabled"))
    decision = check_asset(request, db, config, geo, asset, admin=admin)
    if not decision.allowed:
        if decision.reason == "region":
            raise HTTPException(status_code=451, detail=tr(request, "error.region_short"))
        raise HTTPException(status_code=403, detail=tr(request, "error.asset_locked"))
    key_field = {"thumb": "thumb_key", "preview": "preview_key", "original": "storage_key"}.get(kind)
    if not key_field:
        raise HTTPException(status_code=404)
    # Turning previews off must also stop serving old preview files. Falling
    # back to the original makes the switch effective immediately without
    # requiring a destructive cleanup first.
    served_kind = kind
    if kind == "preview" and get_setting(db, "preview_enabled", "1") != "1":
        served_kind = "original"
        key_field = "storage_key"
    key = asset[key_field]
    if not key:
        key = asset["storage_key"]
        served_kind = "original"
    path = storage.path_for(key)
    if not path or not path.exists():
        raise HTTPException(status_code=404, detail=tr(request, "error.file_missing"))
    effective = asset_effective(db, asset)
    restricted = bool(effective.password_hash) or effective.region_mode in {"allow", "block"} or effective.nsfw
    version = (request.query_params.get("v") or "").strip()
    versioned = bool(version and asset["sha256"].startswith(version))
    headers = {
        "Cache-Control": (
            "private, no-store"
            if restricted
            else (
                "private, max-age=31536000, immutable"
                if versioned
                else "private, max-age=3600"
            )
        ),
        "X-Content-Type-Options": "nosniff",
        "ETag": f'"{asset["sha256"]}-{served_kind}"',
    }
    media_type = (
        "image/webp" if served_kind in {"thumb", "preview"} else asset["mime"]
    )
    if download:
        headers["Content-Disposition"] = (
            f"attachment; filename*=UTF-8''{quote(safe_display_name(asset['original_name']))}"
        )
        headers["Cache-Control"] = "private, no-store"
    if not asset["encrypted"] and not storage.is_encrypted_file(path):
        return FileResponse(
            path,
            media_type=media_type,
            headers=headers,
            filename=safe_display_name(asset["original_name"]) if download else None,
            content_disposition_type="attachment" if download else "inline",
        )
    data = storage.decrypt_to_bytes(path, True)
    return Response(content=data, media_type=media_type, headers=headers)


@router.get("/media/{kind}/{asset_id}")
def media(
    kind: str,
    asset_id: int,
    request: Request,
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    geo: GeoResolver = Depends(get_geo),
    storage: Storage = Depends(get_storage),
):
    return _media_response(asset_id, kind, request, db, config, geo, storage, download=False)


@router.get("/view/{asset_id}")
def view_original(
    asset_id: int,
    request: Request,
    token: str = "",
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    geo: GeoResolver = Depends(get_geo),
    storage: Storage = Depends(get_storage),
):
    """Serve the original through a short-lived, server-signed link.

    The detail page uses this even when external images are enabled so copying
    the address bar never yields a permanent raw-file URL.  A saved link stops
    working after 15 minutes, and direct /media/original access is blocked for
    visitors while external images are disabled.
    """
    if not verify_view_token(asset_id, token, config):
        raise HTTPException(status_code=403, detail=tr(request, "error.view_expired"))
    response = _media_response(
        asset_id,
        "original",
        request,
        db,
        config,
        geo,
        storage,
        allow_original=True,
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    return response


@router.get("/embed/{asset_id}")
def embed_image(
    asset_id: int,
    request: Request,
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    geo: GeoResolver = Depends(get_geo),
    storage: Storage = Depends(get_storage),
):
    """Raw image endpoint intended for <img> hotlinks from other websites."""
    if not _external_access_allowed(db, request):
        raise HTTPException(status_code=403, detail=tr(request, "error.external_disabled"))
    asset = _asset_by_id(db, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail=tr(request, "error.image_missing"))
    decision = check_asset(request, db, config, geo, asset, admin=False)
    if not decision.allowed:
        if decision.reason == "region":
            raise HTTPException(status_code=451, detail=tr(request, "error.region_short"))
        raise HTTPException(status_code=403, detail=tr(request, "error.asset_locked"))
    response = _media_response(
        asset_id,
        "original",
        request,
        db,
        config,
        geo,
        storage,
    )
    response.headers["Cross-Origin-Resource-Policy"] = "cross-origin"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@router.get("/download/{asset_id}")
def download(
    asset_id: int,
    request: Request,
    token: str = "",
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    geo: GeoResolver = Depends(get_geo),
    storage: Storage = Depends(get_storage),
):
    if not _admin_logged_in(db, request) and not verify_download_token(
        asset_id, token, config
    ):
        raise HTTPException(
            status_code=403, detail=tr(request, "error.download_expired")
        )
    return _media_response(
        asset_id,
        "original",
        request,
        db,
        config,
        geo,
        storage,
        download=True,
        allow_original=True,
    )


@router.get("/about")
def about_page(
    request: Request,
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
):
    settings = site_settings(db)
    if not settings["about_enabled"]:
        raise HTTPException(status_code=404, detail=tr(request, "error.about_disabled"))
    return render(
        request,
        db,
        "about.html",
        {"page_title": settings.get("about_name") or f"{tr(request, 'about.eyebrow')} · {settings['site_name']}"},
    )


@router.get("/branding/{filename}")
def branding_file(
    filename: str,
    config: Config = Depends(get_config),
):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}", filename):
        raise HTTPException(status_code=404)
    root = (Path(config.data_dir) / "branding").resolve()
    path = (root / filename).resolve()
    if path.parent != root or not path.is_file():
        raise HTTPException(status_code=404)
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/favicon.ico")
def favicon():
    return RedirectResponse("/static/favicon.svg?v=1", status_code=307)


@router.get("/healthz")
def healthz():
    return {"ok": True}