from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import sqlite3
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from PIL import Image

from .access import (
    album_chain,
    album_counts,
    album_effective,
    asset_effective,
    asset_query,
    box_counts,
    box_cover,
    child_albums,
    clean_album_segments,
    countries_json,
    descendant_album_ids,
    ensure_album_path,
    parse_countries,
    validate_region_mode,
)
from .config import Config
from .database import audit, connect, get_setting, set_setting, utc_now
from .deps import get_config, get_db_conn, get_geo, get_storage, require_admin, require_csrf
from .duplicates import auto_dedupe, filename_groups, hash_summary, normalized_filename
from .geo import GeoResolver, download_public_database
from .guest_upload import (
    album_quota_mb,
    capacity_view,
    daily_key,
    generate_once_code,
    guest_album_bytes,
    set_fixed_key,
)
from .reset import reset_application_data
from .secretbox import decrypt_config, encrypt_config
from .security import (
    clear_login_throttle,
    clear_unlock_cookie,
    cooldown_text,
    create_session,
    current_admin,
    current_session_token,
    delete_session,
    hash_password,
    login_retry_after,
    password_warning,
    register_login_failure,
    set_session_cookie,
    clear_session_cookie,
    verify_password,
)
from .services import (
    create_asset_from_upload,
    delete_asset,
    reparse_asset,
    safe_display_name,
    set_album_encryption,
    set_asset_encryption,
    set_box_encryption,
)
from .storage import Storage, StorageError, StorageFull
from .storage_backends import BackendError, build_backend
from .trash import (
    expiry_for,
    purge_expired,
    reclaim,
    retention_days,
    stats as trash_stats,
)
from .web import (
    CURSOR_URL_RE,
    INTRO_COLOR_RE,
    INTRO_STYLE_MAP,
    admin_url,
    flash_redirect,
    parse_int,
    raw_settings,
    redirect,
    render,
    safe_next,
)

router = APIRouter()
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{3,64}$")
ADMIN_PATH_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
RESERVED_ADMIN_PATHS = {
    "api",
    "media",
    "static",
    "images",
    "albums",
    "boxes",
    "download",
    "embed",
    "view",
    "unlock",
    "search",
    "browse",
    "favicon.ico",
    "healthz",
    "about",
    "branding",
    "feed",
    "upload",
    "stats",
    "language",
    "admin",
}


def _normalize_ids(value: str) -> str:
    out: list[str] = []
    for part in (value or "").replace("，", ",").split(","):
        part = part.strip()
        if part.isdigit() and int(part) > 0 and part not in out:
            out.append(part)
    return ",".join(out[:200])


def _users_count(db: sqlite3.Connection) -> int:
    return int(db.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"])


def _require_experimental(db: sqlite3.Connection) -> None:
    if get_setting(db, "experimental_features", "0") != "1":
        raise HTTPException(status_code=404, detail="实验性功能未开启")


def _admin_ctx(admin: sqlite3.Row, **extra) -> dict:
    ctx = {"admin": admin}
    ctx.update(extra)
    return ctx


def _asset(db: sqlite3.Connection, asset_id: int) -> sqlite3.Row | None:
    return db.execute(
        asset_query() + "WHERE a.id = ?", (asset_id,)
    ).fetchone()


def _album(db: sqlite3.Connection, album_id: int) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM albums WHERE id = ?", (album_id,)).fetchone()


def _box(db: sqlite3.Connection, box_id: int) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM boxes WHERE id = ?", (box_id,)).fetchone()


def _box_choices(db: sqlite3.Connection) -> list[dict]:
    return [
        dict(row)
        for row in db.execute(
            "SELECT * FROM boxes ORDER BY name COLLATE NOCASE"
        ).fetchall()
    ]


def _album_choices(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute("SELECT * FROM albums ORDER BY name COLLATE NOCASE").fetchall()
    by_parent: dict[int | None, list[sqlite3.Row]] = {}
    for row in rows:
        by_parent.setdefault(row["parent_id"], []).append(row)
    out: list[dict] = []

    def walk(parent_id: int | None, depth: int) -> None:
        for row in by_parent.get(parent_id, []):
            if depth > 12:
                continue
            out.append({"id": row["id"], "name": row["name"], "path": row["path"], "depth": depth})
            walk(row["id"], depth + 1)

    walk(None, 0)
    return out


def _album_tree(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute("SELECT * FROM albums ORDER BY path COLLATE NOCASE").fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["asset_count"], item["nsfw_count"] = album_counts(db, row["id"])
        item["depth"] = row["path"].count("/")
        out.append(item)
    return out


def _rebuild_paths(db: sqlite3.Connection, album_id: int) -> None:
    album = _album(db, album_id)
    if not album:
        return
    parent = _album(db, album["parent_id"]) if album["parent_id"] else None
    new_path = (parent["path"] + "/" if parent else "") + album["name"]
    if new_path != album["path"]:
        conflict = db.execute(
            "SELECT id FROM albums WHERE path = ? AND id != ?", (new_path, album_id)
        ).fetchone()
        if conflict:
            raise ValueError("相册路径冲突，请换一个名称")
        db.execute("UPDATE albums SET path = ? WHERE id = ?", (new_path, album_id))
        album = _album(db, album_id)
    for child in child_albums(db, album_id):
        _rebuild_paths(db, child["id"])


def _set_album_password(db: sqlite3.Connection, album: sqlite3.Row, password: str, clear: bool) -> str | None:
    if clear:
        db.execute("UPDATE albums SET access_password_hash = NULL WHERE id = ?", (album["id"],))
        return None
    if password:
        warning = password_warning(password)
        db.execute(
            "UPDATE albums SET access_password_hash = ? WHERE id = ?",
            (hash_password(password), album["id"]),
        )
        return warning
    return None


def _set_box_password(
    db: sqlite3.Connection, box: sqlite3.Row, password: str, clear: bool
) -> str | None:
    if clear:
        db.execute("UPDATE boxes SET access_password_hash = NULL WHERE id = ?", (box["id"],))
        return None
    if password:
        warning = password_warning(password)
        db.execute(
            "UPDATE boxes SET access_password_hash = ? WHERE id = ?",
            (hash_password(password), box["id"]),
        )
        return warning
    return None


# ---------------------------------------------------------------------------
# Setup / login
# ---------------------------------------------------------------------------


@router.get("/setup")
def setup_page(request: Request, db: sqlite3.Connection = Depends(get_db_conn)):
    if _users_count(db) > 0:
        return redirect(admin_url(request, "/login"))
    return render(request, db, "admin/setup.html", {"admin": None}, status_code=200)


@router.post("/setup")
def setup_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
    site_name: str = Form("Ostensory"),
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
):
    require_csrf(request, csrf, config)
    if _users_count(db) > 0:
        return redirect(admin_url(request, "/login"))
    username = username.strip()
    if not USERNAME_RE.match(username):
        return render(
            request,
            db,
            "admin/setup.html",
            {"admin": None, "setup_error": "用户名需为 3-64 位字母、数字或 _ . @ - 字符。", "username": username},
            status_code=400,
        )
    if not password:
        return render(
            request,
            db,
            "admin/setup.html",
            {"admin": None, "setup_error": "密码不能为空。", "username": username},
            status_code=400,
        )
    if password != password2:
        return render(
            request,
            db,
            "admin/setup.html",
            {"admin": None, "setup_error": "两次输入的密码不一致。", "username": username},
            status_code=400,
        )
    warning = password_warning(password)
    now = utc_now()
    cursor = db.execute(
        "INSERT INTO users(username, password_hash, created_at, updated_at) VALUES(?,?,?,?)",
        (username, hash_password(password), now, now),
    )
    user_id = int(cursor.lastrowid)
    if site_name.strip():
        set_setting(db, "site_name", site_name.strip()[:80])
    token, _ = create_session(db, user_id, request, config)
    audit(db, "setup", actor=username, ip=getattr(request.state, "client_ip", ""))
    db.commit()
    destination = admin_url(request, "")
    if warning:
        destination += ("&" if "?" in destination else "?") + "msg=" + quote(warning)
    response = RedirectResponse(destination, status_code=303)
    set_session_cookie(response, request, config, token)
    return response


@router.get("/login")
def login_page(request: Request, next: str = "", db: sqlite3.Connection = Depends(get_db_conn)):
    if _users_count(db) == 0:
        return redirect(admin_url(request, "/setup"))
    return render(
        request,
        db,
        "admin/login.html",
        {"admin": None, "next": safe_next(next, admin_url(request, ""))},
    )


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form(""),
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
):
    require_csrf(request, csrf, config)
    if _users_count(db) == 0:
        return redirect(admin_url(request, "/setup"))
    ip = getattr(request.state, "client_ip", "") or (request.client.host if request.client else "")
    retry_after = login_retry_after(db, ip)
    if retry_after:
        response = render(
            request,
            db,
            "admin/login.html",
            {
                "admin": None,
                "login_error": f"尝试次数过多，请在 {cooldown_text(retry_after)} 后再试。",
                "username": username,
            },
            status_code=429,
        )
        response.headers["Retry-After"] = str(retry_after)
        return response
    user = db.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()
    if not user or not verify_password(password, user["password_hash"]):
        delay = register_login_failure(db, ip)
        if delay:
            response = render(
                request,
                db,
                "admin/login.html",
                {
                    "admin": None,
                    "login_error": f"尝试次数过多，请在 {cooldown_text(delay)} 后再试。",
                    "username": username,
                },
                status_code=429,
            )
            response.headers["Retry-After"] = str(delay)
            return response
        return render(
            request,
            db,
            "admin/login.html",
            {"admin": None, "login_error": "用户名或密码不正确。", "username": username},
            status_code=401,
        )
    clear_login_throttle(db, ip)
    token, _ = create_session(db, user["id"], request, config)
    audit(db, "login", actor=user["username"], ip=ip)
    db.commit()
    destination = safe_next(next, admin_url(request, ""))
    response = RedirectResponse(destination, status_code=303)
    set_session_cookie(response, request, config, token)
    return response


@router.post("/logout")
def logout(
    request: Request,
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
):
    require_csrf(request, csrf, config)
    delete_session(db, current_session_token(request))
    db.commit()
    response = RedirectResponse(admin_url(request, "/login"), status_code=303)
    clear_session_cookie(response)
    return response


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@router.get("")
@router.get("/")
def dashboard(
    request: Request,
    db: sqlite3.Connection = Depends(get_db_conn),
    admin: sqlite3.Row = Depends(require_admin),
    geo: GeoResolver = Depends(get_geo),
):
    stats = {
        "assets": db.execute("SELECT COUNT(*) AS n FROM assets WHERE deleted_at IS NULL").fetchone()["n"],
        "albums": db.execute("SELECT COUNT(*) AS n FROM albums").fetchone()["n"],
        "nsfw": db.execute(
            "SELECT COUNT(*) AS n FROM assets WHERE deleted_at IS NULL AND nsfw = 1"
        ).fetchone()["n"],
        "encrypted": db.execute(
            "SELECT COUNT(*) AS n FROM assets a JOIN blobs b ON b.id = a.blob_id "
            "WHERE a.deleted_at IS NULL AND b.encrypted = 1"
        ).fetchone()["n"],
        "tokens": db.execute(
            "SELECT COUNT(*) AS n FROM api_tokens WHERE revoked_at IS NULL"
        ).fetchone()["n"],
        "bytes": db.execute(
            "SELECT COALESCE(SUM(b.size), 0) AS n FROM assets a JOIN blobs b ON b.id = a.blob_id "
            "WHERE a.deleted_at IS NULL"
        ).fetchone()["n"],
    }
    recent = db.execute(
        asset_query() + "WHERE a.deleted_at IS NULL ORDER BY a.id DESC LIMIT 8"
    ).fetchall()
    recent_logs = db.execute(
        "SELECT * FROM audit_log ORDER BY id DESC LIMIT 12"
    ).fetchall()
    geo_status = geo.status()
    warnings = []
    if (
        get_setting(db, "experimental_features", "0") == "1"
        and not geo_status.available
        and parse_countries(get_setting(db, "nsfw_blocked_countries", "CN"))
    ):
        warnings.append(
            "GeoIP 数据库不可用：NSFW 国家/地区限制会按失败关闭策略执行，可能拦截所有访客。"
        )
    return render(
        request,
        db,
        "admin/dashboard.html",
        _admin_ctx(
            admin,
            stats=stats,
            recent=recent,
            recent_logs=recent_logs,
            geo_status=geo_status,
            warnings=warnings,
            active="dashboard",
        ),
    )


# ---------------------------------------------------------------------------
# Duplicate check
# ---------------------------------------------------------------------------


def _duplicate_context(
    db: sqlite3.Connection,
    admin: sqlite3.Row,
    verify_key: str = "",
    review: bool = False,
) -> dict:
    completed = get_setting(db, "duplicate_check_completed", "0") == "1"
    groups = filename_groups(db) if completed else []
    verify_key = normalized_filename(verify_key)
    for group in groups:
        group["verified"] = bool(verify_key) and group["key"] == verify_key
        group["hash_summary"] = hash_summary(group["assets"]) if group["verified"] else None
    total_assets = sum(group["count"] for group in groups)
    return _admin_ctx(
        admin,
        groups=groups,
        group_count=len(groups),
        duplicate_assets=total_assets,
        check_completed=completed,
        checked_at=get_setting(db, "duplicate_checked_at", ""),
        display_duplicates=get_setting(db, "show_duplicate_images", "1") == "1",
        verified_key=verify_key,
        review_open=review or bool(verify_key),
        active="duplicates",
    )


@router.get("/duplicates")
def duplicates_page(
    request: Request,
    verify: str = "",
    review: str = "",
    db: sqlite3.Connection = Depends(get_db_conn),
    admin: sqlite3.Row = Depends(require_admin),
):
    return render(
        request,
        db,
        "admin/duplicates.html",
        _duplicate_context(db, admin, verify, review == "1"),
    )


@router.post("/duplicates/check")
def duplicates_check(
    request: Request,
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    admin: sqlite3.Row = Depends(require_admin),
):
    require_csrf(request, csrf, config)
    first_check = get_setting(db, "duplicate_check_completed", "0") != "1"
    set_setting(db, "duplicate_check_completed", "1")
    set_setting(db, "duplicate_checked_at", utc_now())
    if first_check:
        set_setting(db, "show_duplicate_images", "1")
    groups = filename_groups(db)
    audit(
        db,
        "duplicates.check",
        actor=admin["username"],
        detail=f"groups={len(groups)}",
        ip=getattr(request.state, "client_ip", ""),
    )
    db.commit()
    return flash_redirect(
        admin_url(request, "/duplicates"),
        f"重复检查完成：发现 {len(groups)} 组同名图片。",
    )


@router.post("/duplicates/display")
def duplicates_display(
    request: Request,
    show: str = Form(""),
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    admin: sqlite3.Row = Depends(require_admin),
):
    require_csrf(request, csrf, config)
    set_setting(db, "show_duplicate_images", "1" if show == "1" else "0")
    audit(
        db,
        "duplicates.display",
        actor=admin["username"],
        detail="show=1" if show == "1" else "show=0",
        ip=getattr(request.state, "client_ip", ""),
    )
    db.commit()
    return flash_redirect(
        admin_url(request, "/duplicates"),
        "已更新重复图片展示设置。",
    )


@router.post("/duplicates/delete")
def duplicates_delete(
    request: Request,
    asset_ids: list[str] = Form([]),
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    storage: Storage = Depends(get_storage),
    admin: sqlite3.Row = Depends(require_admin),
):
    require_csrf(request, csrf, config)
    ids: list[int] = []
    for value in asset_ids:
        if str(value).strip().isdigit():
            number = int(str(value).strip())
            if number > 0 and number not in ids:
                ids.append(number)
    deleted = 0
    for asset_id in ids[:500]:
        asset = db.execute(
            "SELECT * FROM assets WHERE id = ? AND deleted_at IS NULL", (asset_id,)
        ).fetchone()
        if not asset:
            continue
        delete_asset(db, storage, asset_id, purge=False)
        deleted += 1
    audit(
        db,
        "duplicates.delete",
        actor=admin["username"],
        detail=f"deleted={deleted}",
        ip=getattr(request.state, "client_ip", ""),
    )
    db.commit()
    return flash_redirect(
        admin_url(request, "/duplicates") + "?review=1",
        f"已将 {deleted} 张选中的重复图片移入回收站。",
    )


@router.post("/duplicates/auto")
def duplicates_auto(
    request: Request,
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    admin: sqlite3.Row = Depends(require_admin),
):
    require_csrf(request, csrf, config)
    result = auto_dedupe(db)
    set_setting(db, "duplicate_check_completed", "1")
    set_setting(db, "duplicate_checked_at", utc_now())
    audit(
        db,
        "duplicates.auto",
        actor=admin["username"],
        detail=f"deleted={result['deleted']}",
        ip=getattr(request.state, "client_ip", ""),
    )
    db.commit()
    return flash_redirect(
        admin_url(request, "/duplicates"),
        f"一键去重完成：同名且 Hash 相同的旧副本已移入回收站 {result['deleted']} 张，每组保留最新一张。",
    )


@router.get("/duplicates/album-names")
def duplicates_album_names(
    album_id: str = "",
    db: sqlite3.Connection = Depends(get_db_conn),
    admin: sqlite3.Row = Depends(require_admin),
):
    target = None
    if album_id.strip().isdigit():
        target = _album(db, int(album_id))
    else:
        target = ensure_album_path(
            db,
            get_setting(db, "default_album_path", "未分类"),
            create=False,
        )
    if not target:
        return JSONResponse({"album_id": None, "names": []})
    rows = db.execute(
        "SELECT original_name FROM assets WHERE album_id = ? AND deleted_at IS NULL",
        (target["id"],),
    ).fetchall()
    names = sorted(
        {
            normalized_filename(row["original_name"])
            for row in rows
            if normalized_filename(row["original_name"])
        }
    )
    return JSONResponse({"album_id": int(target["id"]), "names": names})


# ---------------------------------------------------------------------------
# Recycle bin
# ---------------------------------------------------------------------------


@router.get("/trash")
def trash_page(
    request: Request,
    page: int = 1,
    db: sqlite3.Connection = Depends(get_db_conn),
    storage: Storage = Depends(get_storage),
    admin: sqlite3.Row = Depends(require_admin),
):
    purge_expired(db, storage)
    db.commit()
    per_page = 40
    page = max(1, page)
    total = db.execute(
        "SELECT COUNT(*) AS n FROM assets WHERE deleted_at IS NOT NULL"
    ).fetchone()["n"]
    rows = db.execute(
        asset_query()
        + "WHERE a.deleted_at IS NOT NULL "
        "ORDER BY a.deleted_at DESC, a.id DESC LIMIT ? OFFSET ?",
        (per_page, (page - 1) * per_page),
    ).fetchall()
    days = retention_days(db)
    expiry_map = {
        int(row["id"]): expiry_for(row["deleted_at"], days) for row in rows
    }
    return render(
        request,
        db,
        "admin/trash.html",
        _admin_ctx(
            admin,
            assets=rows,
            expiry_map=expiry_map,
            retention_days=days,
            trash_stats=trash_stats(db),
            total=total,
            page=page,
            pages=max(1, (total + per_page - 1) // per_page),
            active="trash",
        ),
    )


@router.post("/trash/settings")
def trash_settings(
    request: Request,
    days: str = Form("0"),
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    storage: Storage = Depends(get_storage),
    admin: sqlite3.Row = Depends(require_admin),
):
    require_csrf(request, csrf, config)
    value = parse_int(days, 0, 0, 36500)
    set_setting(db, "trash_retention_days", str(value))
    audit(
        db,
        "trash.settings",
        actor=admin["username"],
        detail=f"days={value}",
    )
    db.commit()
    purged = purge_expired(db, storage)
    db.commit()
    message = (
        "回收站自动删除已关闭，图片会一直保留。"
        if value == 0
        else f"回收站图片会在移入 {value} 天后自动永久删除。"
    )
    if purged["deleted"]:
        message += f" 同时清理了 {purged['deleted']} 张已过期图片。"
    return flash_redirect(admin_url(request, "/trash"), message)


@router.post("/trash/purge")
def trash_purge(
    request: Request,
    asset_ids: list[str] = Form([]),
    only: str = Form(""),
    all: str = Form(""),
    next: str = Form(""),
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    storage: Storage = Depends(get_storage),
    admin: sqlite3.Row = Depends(require_admin),
):
    require_csrf(request, csrf, config)
    destination = safe_next(next, admin_url(request, "/trash"))
    ids: list[int] = []
    if only.strip().isdigit() and int(only.strip()) > 0:
        ids = [int(only.strip())]
    else:
        for value in asset_ids:
            if str(value).strip().isdigit():
                number = int(str(value).strip())
                if number > 0 and number not in ids:
                    ids.append(number)
    if all == "1":
        ids = [
            int(row["id"])
            for row in db.execute(
                "SELECT id FROM assets WHERE deleted_at IS NOT NULL "
                "ORDER BY deleted_at ASC, id ASC LIMIT 10000"
            ).fetchall()
        ]
    if not ids:
        return flash_redirect(destination, "没有选中回收站里的图片。")
    deleted = 0
    failed = 0
    for asset_id in ids:
        row = db.execute(
            "SELECT id FROM assets WHERE id = ? AND deleted_at IS NOT NULL",
            (asset_id,),
        ).fetchone()
        if not row:
            continue
        try:
            delete_asset(db, storage, asset_id, purge=True)
            db.commit()
            deleted += 1
        except Exception:
            db.rollback()
            failed += 1
    audit(
        db,
        "trash.purge",
        actor=admin["username"],
        detail=f"deleted={deleted} failed={failed} ids={','.join(map(str, ids[:80]))}",
        ip=getattr(request.state, "client_ip", ""),
    )
    db.commit()
    message = f"已从回收站永久删除 {deleted} 张图片，释放对应存储空间。"
    if failed:
        message += f" 有 {failed} 张删除失败，请查看文件权限或远程存储连接。"
    return flash_redirect(destination, message)


@router.post("/trash/reclaim")
def trash_reclaim(
    request: Request,
    need: int = Form(0),
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    storage: Storage = Depends(get_storage),
    admin: sqlite3.Row = Depends(require_admin),
):
    require_csrf(request, csrf, config)
    result = reclaim(db, storage, need)
    db.commit()
    audit(
        db,
        "trash.reclaim",
        actor=admin["username"],
        detail=(
            f"deleted={result['deleted']} failed={result['failed']} "
            f"freed_bytes={result['freed_bytes']} ok={result['ok']}"
        ),
        ip=getattr(request.state, "client_ip", ""),
    )
    db.commit()
    result["trash"] = trash_stats(db)
    if result["ok"]:
        if result["deleted"]:
            result["message"] = (
                f"已自动永久删除 {result['deleted']} 张回收站图片，"
                "现在可以重新上传。"
            )
        else:
            result["message"] = "空间已经足够，可以重新上传。"
    else:
        result["message"] = (
            "回收站已经清理，但仍没有足够的存储空间。"
            "请手动删除图片、扩容存储，或启用另一种存储方式。"
        )
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------


@router.get("/assets")
def assets_page(
    request: Request,
    q: str = "",
    ip: str = "",
    album_id: str = "",
    box_id: str = "",
    nsfw: str = "",
    deleted: str = "",
    page: int = 1,
    db: sqlite3.Connection = Depends(get_db_conn),
    admin: sqlite3.Row = Depends(require_admin),
):
    per_page = 40
    page = max(1, page)
    where = []
    params: list = []
    if deleted == "1":
        where.append("a.deleted_at IS NOT NULL")
    else:
        where.append("a.deleted_at IS NULL")
    if q.strip():
        like = f"%{q.strip()[:200]}%"
        where.append(
            "(a.title LIKE ? OR a.original_name LIKE ? OR a.positive_prompt LIKE ? OR a.lora_names LIKE ?)"
        )
        params.extend([like, like, like, like])
    if ip.strip():
        where.append("a.upload_ip LIKE ?")
        params.append(f"%{ip.strip()[:64]}%")
    if album_id and album_id.isdigit():
        where.append("a.album_id = ?")
        params.append(int(album_id))
    if box_id and box_id.isdigit():
        where.append("a.box_id = ?")
        params.append(int(box_id))
    if nsfw == "1":
        where.append("a.nsfw = 1")
    elif nsfw == "0":
        where.append("a.nsfw = 0")
    where_sql = " AND ".join(where)
    total = db.execute(
        f"SELECT COUNT(*) AS n FROM assets a WHERE {where_sql}", params
    ).fetchone()["n"]
    rows = db.execute(
        asset_query()
        + f"WHERE {where_sql} ORDER BY a.id DESC LIMIT ? OFFSET ?",
        params + [per_page, (page - 1) * per_page],
    ).fetchall()
    metadata_map = {}
    for row in rows:
        try:
            metadata_map[row["id"]] = json.loads(row["metadata_json"] or "{}")
        except ValueError:
            metadata_map[row["id"]] = {}
    return render(
        request,
        db,
        "admin/assets.html",
        _admin_ctx(
            admin,
            assets=rows,
            metadata_map=metadata_map,
            albums=_album_choices(db),
            boxes=_box_choices(db),
            total=total,
            page=page,
            pages=max(1, (int(total) + per_page - 1) // per_page),
            q=q,
            ip_filter=ip,
            album_filter=album_id,
            box_filter=box_id,
            nsfw_filter=nsfw,
            deleted_filter=deleted,
            active="assets",
        ),
    )


@router.post("/upload")
def upload_assets(
    request: Request,
    files: list[UploadFile] = File(...),
    album_id: str = Form(""),
    box_id: str = Form(""),
    nsfw: str = Form(""),
    encrypt: str = Form(""),
    region_mode: str = Form("inherit"),
    region_countries: str = Form(""),
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    storage: Storage = Depends(get_storage),
    admin: sqlite3.Row = Depends(require_admin),
):
    require_csrf(request, csrf, config)
    if not files:
        return flash_redirect(admin_url(request, "/assets"), "没有选择文件。")
    if len(files) > 100:
        return flash_redirect(admin_url(request, "/assets"), "一次最多上传 100 个文件。")
    target_album = None
    if album_id.strip().isdigit():
        target_album = _album(db, int(album_id.strip()))
        if not target_album:
            return flash_redirect(admin_url(request, "/assets"), "目标相册不存在。")
    if target_album is None:
        target_album = ensure_album_path(
            db, get_setting(db, "default_album_path", "未分类") or "未分类", create=True
        )
        assert target_album is not None
    target_box = None
    if box_id.strip():
        if not box_id.strip().isdigit():
            return flash_redirect(admin_url(request, "/assets"), "目标匣子无效。")
        target_box = _box(db, int(box_id.strip()))
        if not target_box:
            return flash_redirect(admin_url(request, "/assets"), "目标匣子不存在。")
    purge_expired(db, storage)
    db.commit()
    created = 0
    duplicates = 0
    errors: list[str] = []
    storage_full = False
    if get_setting(db, "experimental_features", "0") != "1":
        region_mode = "inherit"
        region_countries = ""
    for upload in files:
        if not upload.filename:
            continue
        try:
            asset, status = create_asset_from_upload(
                db,
                storage,
                upload.file,
                upload.filename,
                target_album["id"],
                box_id=target_box["id"] if target_box else None,
                source_name="web",
                nsfw=nsfw == "1",
                region_mode=region_mode,
                region_countries=region_countries,
            )
            if encrypt == "1":
                set_asset_encryption(db, storage, asset, True)
            created += 1
            if status == "duplicate":
                duplicates += 1
        except StorageFull as exc:
            storage_full = True
            errors.append(f"{upload.filename}: {exc}")
            break
        except (StorageError, ValueError, OSError) as exc:
            errors.append(f"{upload.filename}: {exc}")
    audit(
        db,
        "assets.upload",
        actor=admin["username"],
        target=str(target_album["id"]),
        detail=f"created={created} errors={len(errors)}",
        ip=getattr(request.state, "client_ip", ""),
    )
    message = f"已处理 {created} 个文件"
    if duplicates:
        message += f"，其中 {duplicates} 个已存在"
    if errors:
        message += "；失败：" + "；".join(errors[:3])
    db.commit()
    suffix = f"/assets?album_id={target_album['id']}"
    if storage_full:
        suffix += "&storage_full=1"
    return flash_redirect(admin_url(request, suffix), message)


@router.post("/assets/bulk-delete")
def assets_bulk_delete(
    request: Request,
    asset_ids: list[str] = Form([]),
    purge: str = Form(""),
    next: str = Form(""),
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    storage: Storage = Depends(get_storage),
    admin: sqlite3.Row = Depends(require_admin),
):
    require_csrf(request, csrf, config)
    destination = safe_next(next, admin_url(request, "/assets"))
    ids: list[int] = []
    for value in asset_ids:
        if str(value).strip().isdigit():
            number = int(str(value).strip())
            if number > 0 and number not in ids:
                ids.append(number)
    ids = ids[:500]
    if not ids:
        return flash_redirect(destination, "没有选中任何图片。")
    deleted = 0
    skipped = 0
    for asset_id in ids:
        asset = db.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if not asset:
            skipped += 1
            continue
        if purge == "1" and not asset["deleted_at"]:
            # The normal list only offers purge in the recycle bin; refuse to
            # turn a bulk soft-delete into permanent deletion by request data.
            skipped += 1
            continue
        delete_asset(db, storage, asset_id, purge=purge == "1")
        deleted += 1
    audit(
        db,
        "assets.bulk_delete",
        actor=admin["username"],
        detail=f"purge={purge == '1'} ids={','.join(map(str, ids[:80]))}",
        ip=getattr(request.state, "client_ip", ""),
    )
    db.commit()
    message = f"已{'彻底' if purge == '1' else '软'}删除 {deleted} 张图片。"
    if skipped:
        message += f" 跳过 {skipped} 张（不存在或不在回收站）。"
    return flash_redirect(destination, message)


@router.get("/assets/{asset_id}")
def asset_edit_page(
    asset_id: int,
    request: Request,
    db: sqlite3.Connection = Depends(get_db_conn),
    admin: sqlite3.Row = Depends(require_admin),
):
    asset = _asset(db, asset_id)
    if not asset or asset["deleted_at"]:
        raise HTTPException(status_code=404, detail="图片不存在")
    try:
        metadata = json.loads(asset["metadata_json"] or "{}")
    except ValueError:
        metadata = {}
    effective = asset_effective(db, asset)
    return render(
        request,
        db,
        "admin/asset_edit.html",
        _admin_ctx(
            admin,
            asset=asset,
            metadata=metadata,
            effective=effective,
            albums=_album_choices(db),
            boxes=_box_choices(db),
            active="assets",
        ),
    )


@router.post("/assets/{asset_id}")
def asset_edit_submit(
    asset_id: int,
    request: Request,
    title: str = Form(""),
    description: str = Form(""),
    intro_font_size: str = Form("15"),
    intro_font_style: str = Form("italic"),
    intro_quote: str = Form(""),
    intro_color: str = Form(""),
    intro_allow_html: str = Form(""),
    album_id: str = Form(""),
    box_id: str = Form(""),
    nsfw: str = Form(""),
    region_mode: str = Form("inherit"),
    region_countries: str = Form(""),
    password: str = Form(""),
    clear_password: str = Form(""),
    encrypt: str = Form(""),
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    storage: Storage = Depends(get_storage),
    admin: sqlite3.Row = Depends(require_admin),
):
    require_csrf(request, csrf, config)
    asset = _asset(db, asset_id)
    intro_size = parse_int(intro_font_size, 15, 10, 72)
    intro_style = intro_font_style if intro_font_style in INTRO_STYLE_MAP else "italic"
    intro_color_value = intro_color.strip()[:40]
    color_invalid = bool(intro_color_value) and not INTRO_COLOR_RE.fullmatch(intro_color_value)
    if color_invalid:
        intro_color_value = ""
    if not asset or asset["deleted_at"]:
        raise HTTPException(status_code=404, detail="图片不存在")
    new_album_id = int(album_id) if album_id.strip().isdigit() else None
    if new_album_id is not None and not _album(db, new_album_id):
        return flash_redirect(admin_url(request, f"/assets/{asset_id}"), "目标相册不存在。")
    new_box_id = int(box_id) if box_id.strip().isdigit() else None
    if new_box_id is not None and not _box(db, new_box_id):
        return flash_redirect(admin_url(request, f"/assets/{asset_id}"), "目标匣子不存在。")
    password_hash = asset["access_password_hash"]
    password_warning_text = None
    if clear_password == "1":
        password_hash = None
    elif password:
        password_warning_text = password_warning(password)
        password_hash = hash_password(password)
    experimental = get_setting(db, "experimental_features", "0") == "1"
    if not experimental:
        region_mode = "inherit"
        region_countries = ""
    want_encrypt = encrypt == "1"
    if want_encrypt != bool(asset["encrypted"]):
        try:
            set_asset_encryption(db, storage, asset, want_encrypt)
        except OSError as exc:
            db.rollback()
            return flash_redirect(admin_url(request, f"/assets/{asset_id}"), f"加密切换失败：{exc}")
    db.execute(
        "UPDATE assets SET title = ?, description = ?, intro_font_size = ?, "
        "intro_font_style = ?, intro_quote = ?, intro_color = ?, intro_allow_html = ?, "
        "album_id = ?, box_id = ?, nsfw = ?, "
        "access_password_hash = ?, region_mode = ?, region_countries = ?, updated_at = ? WHERE id = ?",
        (
            title.strip()[:300] or asset["original_name"],
            description.strip()[:5000],
            str(intro_size),
            intro_style,
            1 if intro_quote == "1" else 0,
            intro_color_value,
            1 if intro_allow_html == "1" else 0,
            new_album_id,
            new_box_id,
            1 if nsfw == "1" else 0,
            password_hash,
            validate_region_mode(region_mode),
            countries_json(region_countries),
            utc_now(),
            asset_id,
        ),
    )
    audit(
        db,
        "asset.update",
        actor=admin["username"],
        target=str(asset_id),
        detail=f"nsfw={nsfw} encrypted={want_encrypt}",
        ip=getattr(request.state, "client_ip", ""),
    )
    db.commit()
    message = "保存成功。"
    if password_warning_text:
        message += " " + password_warning_text
    if color_invalid:
        message += " 简介颜色格式无效，已改为跟随主题颜色。"
    return flash_redirect(admin_url(request, f"/assets/{asset_id}"), message)


@router.post("/assets/{asset_id}/reparse")
def asset_reparse(
    asset_id: int,
    request: Request,
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    storage: Storage = Depends(get_storage),
    admin: sqlite3.Row = Depends(require_admin),
):
    require_csrf(request, csrf, config)
    asset = _asset(db, asset_id)
    if not asset:
        raise HTTPException(status_code=404)
    try:
        reparse_asset(db, storage, asset)
    except (OSError, ValueError) as exc:
        return flash_redirect(admin_url(request, f"/assets/{asset_id}"), f"解析失败：{exc}")
    audit(db, "asset.reparse", actor=admin["username"], target=str(asset_id))
    db.commit()
    return flash_redirect(admin_url(request, f"/assets/{asset_id}"), "已重新解析元数据。")


@router.post("/reparse-all")
def assets_reparse_all(
    request: Request,
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    storage: Storage = Depends(get_storage),
    admin: sqlite3.Row = Depends(require_admin),
):
    require_csrf(request, csrf, config)
    rows = db.execute(
        "SELECT a.*, b.storage_key, b.width, b.height, b.encrypted "
        "FROM assets a JOIN blobs b ON b.id = a.blob_id WHERE a.deleted_at IS NULL"
    ).fetchall()
    ok = 0
    failed = 0
    for row in rows:
        try:
            reparse_asset(db, storage, row)
            ok += 1
        except (OSError, ValueError):
            failed += 1
    audit(db, "assets.reparse_all", actor=admin["username"], detail=f"ok={ok} failed={failed}")
    db.commit()
    message = f"已重新解析 {ok} 张图片"
    if failed:
        message += f"，失败 {failed} 张"
    return flash_redirect(admin_url(request, "/assets"), message)


@router.post("/assets/{asset_id}/delete")
def asset_delete(
    asset_id: int,
    request: Request,
    purge: str = Form(""),
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    storage: Storage = Depends(get_storage),
    admin: sqlite3.Row = Depends(require_admin),
):
    require_csrf(request, csrf, config)
    asset = _asset(db, asset_id)
    if not asset:
        raise HTTPException(status_code=404)
    delete_asset(db, storage, asset_id, purge=purge == "1")
    audit(
        db,
        "asset.purge" if purge == "1" else "asset.delete",
        actor=admin["username"],
        target=str(asset_id),
    )
    db.commit()
    return flash_redirect(admin_url(request, "/assets"), "已删除。" if purge != "1" else "已彻底删除。")


# ---------------------------------------------------------------------------
# Albums
# ---------------------------------------------------------------------------


@router.get("/boxes")
def boxes_admin(
    request: Request,
    db: sqlite3.Connection = Depends(get_db_conn),
    admin: sqlite3.Row = Depends(require_admin),
):
    rows = []
    for row in db.execute("SELECT * FROM boxes ORDER BY name COLLATE NOCASE").fetchall():
        item = dict(row)
        item["direct_count"], item["album_count"] = box_counts(db, int(row["id"]))
        item["cover"] = box_cover(db, int(row["id"]))
        rows.append(item)
    return render(
        request,
        db,
        "admin/boxes.html",
        _admin_ctx(admin, boxes=rows, active="boxes"),
    )


@router.post("/boxes")
def box_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    password: str = Form(""),
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    admin: sqlite3.Row = Depends(require_admin),
):
    require_csrf(request, csrf, config)
    cleaned = name.strip()[:80]
    if not cleaned:
        return flash_redirect(admin_url(request, "/boxes"), "匣子名称不能为空。")
    if db.execute("SELECT id FROM boxes WHERE name = ?", (cleaned,)).fetchone():
        return flash_redirect(admin_url(request, "/boxes"), "同名匣子已存在。")
    now = utc_now()
    cursor = db.execute(
        "INSERT INTO boxes(name, description, access_password_hash, created_at, updated_at) "
        "VALUES(?,?,?,?,?)",
        (
            cleaned,
            description.strip()[:2000],
            hash_password(password) if password else None,
            now,
            now,
        ),
    )
    audit(db, "box.create", actor=admin["username"], target=str(cursor.lastrowid))
    db.commit()
    message = "匣子已创建。"
    if password:
        warning = password_warning(password)
        if warning:
            message += " " + warning
    return flash_redirect(admin_url(request, f"/boxes/{cursor.lastrowid}"), message)


@router.get("/boxes/{box_id}")
def box_edit_page(
    box_id: int,
    request: Request,
    album_q: str = "",
    image_q: str = "",
    current_album_q: str = "",
    current_image_q: str = "",
    db: sqlite3.Connection = Depends(get_db_conn),
    admin: sqlite3.Row = Depends(require_admin),
):
    box = _box(db, box_id)
    if not box:
        raise HTTPException(status_code=404)
    album_query = (album_q or "").strip()[:120]
    image_query = (image_q or "").strip()[:120]
    current_album_query = (current_album_q or "").strip()[:120]
    current_image_query = (current_image_q or "").strip()[:120]
    album_like = f"%{album_query}%"
    image_like = f"%{image_query}%"
    current_album_like = f"%{current_album_query}%"
    current_image_like = f"%{current_image_query}%"

    album_total = int(
        db.execute("SELECT COUNT(*) AS n FROM albums WHERE box_id = ?", (box_id,)).fetchone()["n"]
    )
    asset_total = int(
        db.execute(
            "SELECT COUNT(*) AS n FROM assets WHERE box_id = ? AND deleted_at IS NULL",
            (box_id,),
        ).fetchone()["n"]
    )
    album_rows = db.execute(
        "SELECT * FROM albums WHERE box_id = ? "
        "AND (? = '' OR name LIKE ? OR path LIKE ?) "
        "ORDER BY path COLLATE NOCASE",
        (box_id, current_album_query, current_album_like, current_album_like),
    ).fetchall()
    albums = []
    for row in album_rows:
        item = dict(row)
        item["asset_count"], _ = album_counts(db, int(row["id"]))
        albums.append(item)
    assets = db.execute(
        asset_query()
        + "WHERE a.box_id = ? AND a.deleted_at IS NULL "
        "AND (? = '' OR a.title LIKE ? OR a.original_name LIKE ? OR CAST(a.id AS TEXT) = ?) "
        "ORDER BY a.id DESC",
        (box_id, current_image_query, current_image_like, current_image_like, current_image_query),
    ).fetchall()

    album_candidates = db.execute(
        "SELECT al.*, bx.name AS current_box_name FROM albums al "
        "LEFT JOIN boxes bx ON bx.id = al.box_id "
        "WHERE (al.box_id IS NULL OR al.box_id != ?) "
        "AND (? = '' OR al.name LIKE ? OR al.path LIKE ?) "
        "ORDER BY al.path COLLATE NOCASE LIMIT 160",
        (box_id, album_query, album_like, album_like),
    ).fetchall()
    box_album_ids = [
        int(row["id"])
        for row in db.execute(
            "WITH RECURSIVE box_albums(id) AS ("
            " SELECT id FROM albums WHERE box_id = ?"
            " UNION SELECT al.id FROM albums al JOIN box_albums b ON al.parent_id = b.id"
            ") SELECT id FROM box_albums",
            (box_id,),
        ).fetchall()
    ]
    candidate_sql = (
        asset_query()
        + "WHERE a.deleted_at IS NULL AND (a.box_id IS NULL OR a.box_id != ?) "
    )
    candidate_params: list[object] = [box_id]
    if box_album_ids:
        candidate_sql += (
            "AND (a.album_id IS NULL OR a.album_id NOT IN ("
            + ",".join("?" for _ in box_album_ids)
            + ")) "
        )
        candidate_params.extend(box_album_ids)
    candidate_sql += (
        "AND (? = '' OR a.title LIKE ? OR a.original_name LIKE ? OR CAST(a.id AS TEXT) = ?) "
        "ORDER BY a.id DESC LIMIT 120"
    )
    candidate_params.extend([image_query, image_like, image_like, image_query])
    image_candidates = db.execute(candidate_sql, candidate_params).fetchall()
    return render(
        request,
        db,
        "admin/box_edit.html",
        _admin_ctx(
            admin,
            box=box,
            albums=albums,
            assets=assets,
            album_total=album_total,
            asset_total=asset_total,
            album_candidates=album_candidates,
            image_candidates=image_candidates,
            album_query=album_query,
            image_query=image_query,
            current_album_query=current_album_query,
            current_image_query=current_image_query,
            active="boxes",
        ),
    )


@router.post("/boxes/{box_id}")
def box_edit_submit(
    box_id: int,
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    cover_asset_id: str = Form(""),
    password: str = Form(""),
    clear_password: str = Form(""),
    encryption_action: str = Form("keep"),
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    storage: Storage = Depends(get_storage),
    admin: sqlite3.Row = Depends(require_admin),
):
    require_csrf(request, csrf, config)
    box = _box(db, box_id)
    if not box:
        raise HTTPException(status_code=404)
    cleaned = name.strip()[:80]
    if not cleaned:
        return flash_redirect(admin_url(request, f"/boxes/{box_id}"), "匣子名称不能为空。")
    conflict = db.execute(
        "SELECT id FROM boxes WHERE name = ? AND id != ?", (cleaned, box_id)
    ).fetchone()
    if conflict:
        return flash_redirect(admin_url(request, f"/boxes/{box_id}"), "同名匣子已存在。")
    cover = int(cover_asset_id) if cover_asset_id.strip().isdigit() else None
    if cover is not None:
        valid = db.execute(
            "SELECT id FROM assets WHERE id = ? AND box_id = ? AND deleted_at IS NULL",
            (cover, box_id),
        ).fetchone()
        if not valid:
            cover = None
    try:
        password_warning_text = _set_box_password(db, box, password, clear_password == "1")
        db.execute(
            "UPDATE boxes SET name = ?, description = ?, cover_asset_id = ?, updated_at = ? WHERE id = ?",
            (cleaned, description.strip()[:2000], cover, utc_now(), box_id),
        )
        if encryption_action in {"encrypt", "decrypt"}:
            set_box_encryption(db, storage, box_id, encryption_action == "encrypt")
        db.commit()
    except (ValueError, OSError) as exc:
        db.rollback()
        return flash_redirect(admin_url(request, f"/boxes/{box_id}"), f"保存失败：{exc}")
    audit(db, "box.update", actor=admin["username"], target=str(box_id))
    db.commit()
    message = "保存成功。"
    if password_warning_text:
        message += " " + password_warning_text
    return flash_redirect(admin_url(request, f"/boxes/{box_id}"), message)


def _selected_member_ids(ids: list[int]) -> list[int]:
    return sorted({int(item) for item in ids if int(item) > 0})[:500]


def _bulk_member_update(
    db: sqlite3.Connection,
    *,
    table: str,
    box_id: int,
    ids: list[int],
    attach: bool,
) -> int:
    selected = _selected_member_ids(ids)
    if not selected:
        return 0
    marks = ",".join("?" for _ in selected)
    if attach:
        cursor = db.execute(
            f"UPDATE {table} SET box_id = ? WHERE id IN ({marks})",
            (box_id, *selected),
        )
    else:
        cursor = db.execute(
            f"UPDATE {table} SET box_id = NULL WHERE box_id = ? AND id IN ({marks})",
            (box_id, *selected),
        )
    return int(cursor.rowcount or 0)


@router.post("/boxes/{box_id}/albums/attach")
def box_albums_attach(
    box_id: int,
    request: Request,
    album_ids: list[int] = Form([]),
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    admin: sqlite3.Row = Depends(require_admin),
):
    require_csrf(request, csrf, config)
    if not _box(db, box_id):
        raise HTTPException(status_code=404)
    changed = _bulk_member_update(db, table="albums", box_id=box_id, ids=album_ids, attach=True)
    db.execute("UPDATE boxes SET updated_at = ? WHERE id = ?", (utc_now(), box_id))
    audit(db, "box.albums.attach", actor=admin["username"], target=str(box_id), detail=str(changed))
    db.commit()
    message = f"已加入 {changed} 本相册。" if changed else "没有选择相册。"
    return flash_redirect(admin_url(request, f"/boxes/{box_id}"), message)


@router.post("/boxes/{box_id}/albums/detach")
def box_albums_detach(
    box_id: int,
    request: Request,
    album_ids: list[int] = Form([]),
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    admin: sqlite3.Row = Depends(require_admin),
):
    require_csrf(request, csrf, config)
    if not _box(db, box_id):
        raise HTTPException(status_code=404)
    changed = _bulk_member_update(db, table="albums", box_id=box_id, ids=album_ids, attach=False)
    db.execute("UPDATE boxes SET updated_at = ? WHERE id = ?", (utc_now(), box_id))
    audit(db, "box.albums.detach", actor=admin["username"], target=str(box_id), detail=str(changed))
    db.commit()
    message = f"已将 {changed} 本相册移出匣子。" if changed else "没有选择相册。"
    return flash_redirect(admin_url(request, f"/boxes/{box_id}"), message)


@router.post("/boxes/{box_id}/images/attach")
def box_images_attach(
    box_id: int,
    request: Request,
    asset_ids: list[int] = Form([]),
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    admin: sqlite3.Row = Depends(require_admin),
):
    require_csrf(request, csrf, config)
    if not _box(db, box_id):
        raise HTTPException(status_code=404)
    changed = _bulk_member_update(db, table="assets", box_id=box_id, ids=asset_ids, attach=True)
    db.execute("UPDATE boxes SET updated_at = ? WHERE id = ?", (utc_now(), box_id))
    audit(db, "box.images.attach", actor=admin["username"], target=str(box_id), detail=str(changed))
    db.commit()
    message = f"已加入 {changed} 张图片。" if changed else "没有选择图片。"
    return flash_redirect(admin_url(request, f"/boxes/{box_id}"), message)


@router.post("/boxes/{box_id}/images/detach")
def box_images_detach(
    box_id: int,
    request: Request,
    asset_ids: list[int] = Form([]),
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    admin: sqlite3.Row = Depends(require_admin),
):
    require_csrf(request, csrf, config)
    if not _box(db, box_id):
        raise HTTPException(status_code=404)
    changed = _bulk_member_update(db, table="assets", box_id=box_id, ids=asset_ids, attach=False)
    db.execute("UPDATE boxes SET updated_at = ? WHERE id = ?", (utc_now(), box_id))
    audit(db, "box.images.detach", actor=admin["username"], target=str(box_id), detail=str(changed))
    db.commit()
    message = f"已将 {changed} 张图片移出匣子。" if changed else "没有选择图片。"
    return flash_redirect(admin_url(request, f"/boxes/{box_id}"), message)


@router.post("/boxes/{box_id}/delete")
def box_delete(
    box_id: int,
    request: Request,
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    admin: sqlite3.Row = Depends(require_admin),
):
    require_csrf(request, csrf, config)
    box = _box(db, box_id)
    if not box:
        raise HTTPException(status_code=404)
    db.execute("DELETE FROM boxes WHERE id = ?", (box_id,))
    audit(db, "box.delete", actor=admin["username"], target=str(box_id))
    db.commit()
    return flash_redirect(admin_url(request, "/boxes"), "匣子已删除；里面的相册和图片已退回未分组。")


@router.get("/albums")
def albums_admin(
    request: Request,
    db: sqlite3.Connection = Depends(get_db_conn),
    admin: sqlite3.Row = Depends(require_admin),
):
    return render(
        request,
        db,
        "admin/albums.html",
        _admin_ctx(admin, albums=_album_tree(db), choices=_album_choices(db), boxes=_box_choices(db), active="albums"),
    )


@router.post("/albums")
def album_create(
    request: Request,
    name: str = Form(...),
    parent_id: str = Form(""),
    box_id: str = Form(""),
    description: str = Form(""),
    guest_quota_mb: str = Form("0"),
    nsfw: str = Form(""),
    password: str = Form(""),
    region_mode: str = Form("inherit"),
    region_countries: str = Form(""),
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    admin: sqlite3.Row = Depends(require_admin),
):
    require_csrf(request, csrf, config)
    try:
        segments = clean_album_segments(name)
        if len(segments) != 1:
            raise ValueError("相册名称不能包含 /")
        parent = _album(db, int(parent_id)) if parent_id.strip().isdigit() else None
        target_box = _box(db, int(box_id)) if box_id.strip().isdigit() else None
        path = (parent["path"] + "/" if parent else "") + segments[0]
        if db.execute("SELECT id FROM albums WHERE path = ?", (path,)).fetchone():
            raise ValueError("同名相册已存在")
        password_warning_text = password_warning(password) if password else None
        if get_setting(db, "experimental_features", "0") != "1":
            region_mode = "inherit"
            region_countries = ""
        now = utc_now()
        db.execute(
            "INSERT INTO albums(parent_id, box_id, name, path, description, guest_quota_mb, nsfw, "
            "access_password_hash, region_mode, region_countries, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                parent["id"] if parent else None,
                target_box["id"] if target_box else None,
                segments[0],
                path,
                description.strip()[:2000],
                parse_int(guest_quota_mb, 0, 0, 1024 * 1024),
                1 if nsfw == "1" else 0,
                hash_password(password) if password else None,
                validate_region_mode(region_mode),
                countries_json(region_countries),
                now,
                now,
            ),
        )
        db.commit()
    except ValueError as exc:
        return flash_redirect(admin_url(request, "/albums"), f"创建失败：{exc}")
    audit(db, "album.create", actor=admin["username"], target=path)
    db.commit()
    message = "相册已创建。"
    if password_warning_text:
        message += " " + password_warning_text
    return flash_redirect(admin_url(request, "/albums"), message)


@router.get("/albums/{album_id}")
def album_edit_page(
    album_id: int,
    request: Request,
    db: sqlite3.Connection = Depends(get_db_conn),
    admin: sqlite3.Row = Depends(require_admin),
):
    album = _album(db, album_id)
    if not album:
        raise HTTPException(status_code=404)
    choices = [choice for choice in _album_choices(db) if choice["id"] != album_id]
    blocked = set(descendant_album_ids(db, album_id))
    choices = [choice for choice in choices if choice["id"] not in blocked]
    assets = db.execute(
        asset_query() + "WHERE a.album_id = ? AND a.deleted_at IS NULL ORDER BY a.id DESC LIMIT 200",
        (album_id,),
    ).fetchall()
    return render(
        request,
        db,
        "admin/album_edit.html",
        _admin_ctx(
            admin,
            album=album,
            choices=choices,
            boxes=_box_choices(db),
            assets=assets,
            effective=album_effective(db, album_id),
            guest_capacity=capacity_view(
                album_quota_mb(album), guest_album_bytes(db, album_id)
            ),
            active="albums",
        ),
    )


@router.post("/albums/bulk-delete")
def albums_bulk_delete(
    request: Request,
    album_ids: list[str] = Form([]),
    next: str = Form(""),
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    admin: sqlite3.Row = Depends(require_admin),
):
    require_csrf(request, csrf, config)
    destination = safe_next(next, admin_url(request, "/albums"))
    ids: list[int] = []
    for value in album_ids:
        if str(value).strip().isdigit():
            number = int(str(value).strip())
            if number > 0 and number not in ids:
                ids.append(number)
    ids = ids[:500]
    if not ids:
        return flash_redirect(destination, "没有选中任何相册。")
    rows = db.execute(
        "SELECT * FROM albums WHERE id IN (" + ",".join("?" for _ in ids) + ")",
        tuple(ids),
    ).fetchall()
    # Delete deepest selected albums first so selecting a parent and an empty
    # child together can succeed.
    rows = sorted(rows, key=lambda row: row["path"].count("/"), reverse=True)
    deleted = 0
    skipped: list[str] = []
    for album in rows:
        if child_albums(db, album["id"]):
            skipped.append(album["name"])
            continue
        count = db.execute(
            "SELECT COUNT(*) AS n FROM assets WHERE album_id = ? AND deleted_at IS NULL",
            (album["id"],),
        ).fetchone()["n"]
        if count:
            skipped.append(album["name"])
            continue
        db.execute("DELETE FROM albums WHERE id = ?", (album["id"],))
        audit(db, "album.bulk_delete", actor=admin["username"], target=str(album["id"]))
        deleted += 1
    db.commit()
    message = f"已删除 {deleted} 个空相册。"
    if skipped:
        message += " 未删除（有图片或子相册）：" + "、".join(skipped[:5])
        if len(skipped) > 5:
            message += f" 等 {len(skipped)} 个"
    return flash_redirect(destination, message)


@router.post("/albums/{album_id}")
def album_edit_submit(
    album_id: int,
    request: Request,
    name: str = Form(...),
    parent_id: str = Form(""),
    box_id: str = Form(""),
    description: str = Form(""),
    guest_quota_mb: str = Form("0"),
    nsfw: str = Form(""),
    password: str = Form(""),
    clear_password: str = Form(""),
    region_mode: str = Form("inherit"),
    region_countries: str = Form(""),
    cover_asset_id: str = Form(""),
    encryption_action: str = Form("keep"),
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    storage: Storage = Depends(get_storage),
    admin: sqlite3.Row = Depends(require_admin),
):
    require_csrf(request, csrf, config)
    album = _album(db, album_id)
    if not album:
        raise HTTPException(status_code=404)
    try:
        segments = clean_album_segments(name)
        if len(segments) != 1:
            raise ValueError("相册名称不能包含 /")
        new_parent_id = int(parent_id) if parent_id.strip().isdigit() else None
        target_box = _box(db, int(box_id)) if box_id.strip().isdigit() else None
        if new_parent_id == album_id:
            raise ValueError("不能把相册设为自己的上级")
        if new_parent_id and new_parent_id in set(descendant_album_ids(db, album_id)):
            raise ValueError("不能把相册移动到自己的子相册里")
        new_parent = _album(db, new_parent_id) if new_parent_id else None
        new_path = (new_parent["path"] + "/" if new_parent else "") + segments[0]
        conflict = db.execute(
            "SELECT id FROM albums WHERE path = ? AND id != ?", (new_path, album_id)
        ).fetchone()
        if conflict:
            raise ValueError("相册路径冲突")
        cover_id = int(cover_asset_id) if cover_asset_id.strip().isdigit() else None
        if cover_id:
            cover = db.execute("SELECT id FROM assets WHERE id = ?", (cover_id,)).fetchone()
            if not cover:
                raise ValueError("封面图片不存在")
        password_warning_text = _set_album_password(db, album, password, clear_password == "1")
        if get_setting(db, "experimental_features", "0") != "1":
            region_mode = "inherit"
            region_countries = ""
        db.execute(
            "UPDATE albums SET name = ?, parent_id = ?, box_id = ?, description = ?, guest_quota_mb = ?, "
            "nsfw = ?, region_mode = ?, region_countries = ?, cover_asset_id = ?, updated_at = ? "
            "WHERE id = ?",
            (
                segments[0],
                new_parent_id,
                target_box["id"] if target_box else None,
                description.strip()[:2000],
                parse_int(guest_quota_mb, 0, 0, 1024 * 1024),
                1 if nsfw == "1" else 0,
                validate_region_mode(region_mode),
                countries_json(region_countries),
                cover_id,
                utc_now(),
                album_id,
            ),
        )
        _rebuild_paths(db, album_id)
        if encryption_action in {"encrypt", "decrypt"}:
            set_album_encryption(db, storage, album_id, encryption_action == "encrypt")
        db.commit()
    except (ValueError, OSError) as exc:
        db.rollback()
        return flash_redirect(admin_url(request, f"/albums/{album_id}"), f"保存失败：{exc}")
    audit(db, "album.update", actor=admin["username"], target=str(album_id))
    db.commit()
    message = "相册已保存。"
    if password_warning_text:
        message += " " + password_warning_text
    return flash_redirect(admin_url(request, f"/albums/{album_id}"), message)


@router.post("/albums/{album_id}/delete")
def album_delete(
    album_id: int,
    request: Request,
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    admin: sqlite3.Row = Depends(require_admin),
):
    require_csrf(request, csrf, config)
    album = _album(db, album_id)
    if not album:
        raise HTTPException(status_code=404)
    if child_albums(db, album_id):
        return flash_redirect(admin_url(request, f"/albums/{album_id}"), "请先删除或移走子相册。")
    count = db.execute(
        "SELECT COUNT(*) AS n FROM assets WHERE album_id = ? AND deleted_at IS NULL", (album_id,)
    ).fetchone()["n"]
    if count:
        return flash_redirect(
            admin_url(request, f"/albums/{album_id}"),
            "相册里还有图片，请先移动到其他相册或删除图片。",
        )
    db.execute("DELETE FROM albums WHERE id = ?", (album_id,))
    audit(db, "album.delete", actor=admin["username"], target=str(album_id))
    db.commit()
    return flash_redirect(admin_url(request, "/albums"), "相册已删除。")


# ---------------------------------------------------------------------------
# Settings / account / tokens / geoip
# ---------------------------------------------------------------------------


STORAGE_KINDS = {
    "local": "本机存储",
    "webdav": "WebDAV",
    "s3": "S3 兼容（R2 / OSS / MinIO）",
    "worker": "Cloudflare Worker",
}
STORAGE_SECRET_FIELDS = {"password", "secret_key", "token"}


def _storage_config(row: sqlite3.Row, config: Config) -> dict:
    try:
        value = json.loads(row["config_json"] or "{}")
    except (TypeError, ValueError):
        value = {}
    if not isinstance(value, dict):
        return {}
    try:
        return decrypt_config(config.master_key, value)
    except ValueError:
        # Keep the page readable; storage calls surface the key mismatch.
        return value


def _storage_views(
    db: sqlite3.Connection, storage: Storage, config: Config
) -> tuple[list[dict], int]:
    rows = db.execute(
        "SELECT * FROM storage_backends ORDER BY priority, id"
    ).fetchall()
    local_free = 0
    try:
        local_free = shutil.disk_usage(config.data_dir).free
    except OSError:
        pass
    views: list[dict] = []
    total = 0
    for row in rows:
        item = dict(row)
        used = storage.backend_usage(db, int(row["id"]))
        total += used
        quota = max(0, int(row["quota_mb"] or 0)) * 1024 * 1024
        threshold = max(1, min(100, int(row["threshold_pct"] or 90)))
        cfg = _storage_config(row, config)
        safe_cfg = {
            key: ("" if key in STORAGE_SECRET_FIELDS else value)
            for key, value in cfg.items()
        }
        item.update(
            {
                "config": safe_cfg,
                "secret_set": {
                    key: bool(str(cfg.get(key) or ""))
                    for key in STORAGE_SECRET_FIELDS
                },
                "used_bytes": used,
                "used_mb": round(used / 1024 / 1024, 2),
                "quota_bytes": quota,
                "quota_mb_value": int(row["quota_mb"] or 0),
                "threshold_pct": threshold,
                "percent": round(used * 100 / quota, 1) if quota else 0,
                "remaining_mb": (
                    round(max(0, quota - used) / 1024 / 1024, 2) if quota else None
                ),
                "full": bool(quota and used >= quota),
                "threshold_hit": bool(
                    quota and used * 100 >= quota * threshold
                ),
                "local_free": local_free if row["kind"] == "local" else None,
            }
        )
        views.append(item)
    return views, total


def _merge_storage_config(
    kind: str,
    existing: dict,
    form: dict[str, str],
) -> dict:
    def value(name: str, secret: bool = False) -> str:
        raw = str(form.get(name) or "").strip()
        if secret and not raw:
            return str(existing.get(name) or "")
        return raw

    if kind == "local":
        return {}
    if kind == "webdav":
        return {
            "url": value("cfg_url"),
            "username": value("cfg_username"),
            "password": value("cfg_password", True),
            "prefix": value("cfg_prefix"),
            "verify_tls": form.get("cfg_verify_tls") == "1",
        }
    if kind == "worker":
        return {
            "url": value("cfg_url"),
            "token": value("cfg_token", True),
            "prefix": value("cfg_prefix"),
            "verify_tls": form.get("cfg_verify_tls") == "1",
        }
    if kind == "s3":
        return {
            "endpoint": value("cfg_endpoint"),
            "bucket": value("cfg_bucket"),
            "region": value("cfg_region") or "auto",
            "access_key": value("cfg_access_key"),
            "secret_key": value("cfg_secret_key", True),
            "prefix": value("cfg_prefix"),
            "addressing_style": value("cfg_addressing_style") or "auto",
        }
    raise ValueError("未知存储类型")


@router.get("/storage")
def storage_page(
    request: Request,
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    storage: Storage = Depends(get_storage),
    admin: sqlite3.Row = Depends(require_admin),
):
    storage.reload_backends()
    backends, total_used = _storage_views(db, storage, config)
    return render(
        request,
        db,
        "admin/storage.html",
        _admin_ctx(
            admin,
            backends=backends,
            storage_kinds=STORAGE_KINDS,
            total_used=total_used,
            total_used_mb=round(total_used / 1024 / 1024, 2),
            active="storage",
        ),
    )


@router.get("/storage/capacity")
def storage_capacity(
    request: Request,
    bytes: int = 0,
    db: sqlite3.Connection = Depends(get_db_conn),
    storage: Storage = Depends(get_storage),
    admin: sqlite3.Row = Depends(require_admin),
):
    purge_expired(db, storage)
    db.commit()
    status = storage.capacity_status(db, parse_int(str(bytes), 0, 0, 2 * 1024 * 1024 * 1024))
    status["trash"] = trash_stats(db)
    return JSONResponse(status)


@router.post("/storage/add")
def storage_add(
    request: Request,
    kind: str = Form("worker"),
    name: str = Form(""),
    priority: str = Form("100"),
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    storage: Storage = Depends(get_storage),
    admin: sqlite3.Row = Depends(require_admin),
):
    require_csrf(request, csrf, config)
    if kind not in STORAGE_KINDS or kind == "local":
        return flash_redirect(admin_url(request, "/storage"), "未知存储类型。")
    clean_name = name.strip()[:80] or STORAGE_KINDS[kind]
    now = utc_now()
    db.execute(
        "INSERT INTO storage_backends"
        "(kind, name, enabled, priority, quota_mb, threshold_pct, config_json, created_at, updated_at) "
        "VALUES(?,?,1,?,0,90,'{}',?,?)",
        (kind, clean_name, parse_int(priority, 100, 0, 10000), now, now),
    )
    audit(
        db,
        "storage.add",
        actor=admin["username"],
        target=kind,
        detail=clean_name,
    )
    db.commit()
    storage.reload_backends()
    return flash_redirect(admin_url(request, "/storage"), "已新增存储方式，请填写配置。")


@router.post("/storage/{backend_id}")
def storage_update(
    request: Request,
    backend_id: int,
    name: str = Form(""),
    enabled: str = Form(""),
    priority: str = Form("100"),
    quota_mb: str = Form("0"),
    threshold_pct: str = Form("90"),
    cfg_url: str = Form(""),
    cfg_username: str = Form(""),
    cfg_password: str = Form(""),
    cfg_prefix: str = Form(""),
    cfg_endpoint: str = Form(""),
    cfg_bucket: str = Form(""),
    cfg_region: str = Form("auto"),
    cfg_access_key: str = Form(""),
    cfg_secret_key: str = Form(""),
    cfg_token: str = Form(""),
    cfg_addressing_style: str = Form("auto"),
    cfg_verify_tls: str = Form(""),
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    storage: Storage = Depends(get_storage),
    admin: sqlite3.Row = Depends(require_admin),
):
    require_csrf(request, csrf, config)
    row = db.execute(
        "SELECT * FROM storage_backends WHERE id = ?", (backend_id,)
    ).fetchone()
    if not row:
        return flash_redirect(admin_url(request, "/storage"), "存储方式不存在。")
    existing = _storage_config(row, config)
    form = {
        "cfg_url": cfg_url,
        "cfg_username": cfg_username,
        "cfg_password": cfg_password,
        "cfg_prefix": cfg_prefix,
        "cfg_endpoint": cfg_endpoint,
        "cfg_bucket": cfg_bucket,
        "cfg_region": cfg_region,
        "cfg_access_key": cfg_access_key,
        "cfg_secret_key": cfg_secret_key,
        "cfg_token": cfg_token,
        "cfg_addressing_style": cfg_addressing_style,
        "cfg_verify_tls": cfg_verify_tls,
    }
    try:
        merged = _merge_storage_config(str(row["kind"]), existing, form)
    except ValueError as exc:
        return flash_redirect(admin_url(request, "/storage"), str(exc))
    clean_name = name.strip()[:80] or str(row["name"])
    db.execute(
        "UPDATE storage_backends SET name = ?, enabled = ?, priority = ?, "
        "quota_mb = ?, threshold_pct = ?, config_json = ?, updated_at = ? WHERE id = ?",
        (
            clean_name,
            1 if enabled == "1" else 0,
            parse_int(priority, 100, 0, 10000),
            parse_int(quota_mb, 0, 0, 10_000_000),
            parse_int(threshold_pct, 90, 1, 100),
            json.dumps(
                encrypt_config(config.master_key, merged), ensure_ascii=False
            ),
            utc_now(),
            backend_id,
        ),
    )
    audit(
        db,
        "storage.update",
        actor=admin["username"],
        target=str(backend_id),
        detail=clean_name,
    )
    db.commit()
    storage.reload_backends()
    return flash_redirect(admin_url(request, "/storage"), "存储设置已保存。")


@router.post("/storage/{backend_id}/test")
def storage_test(
    request: Request,
    backend_id: int,
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    storage: Storage = Depends(get_storage),
    admin: sqlite3.Row = Depends(require_admin),
):
    require_csrf(request, csrf, config)
    row = db.execute(
        "SELECT * FROM storage_backends WHERE id = ?", (backend_id,)
    ).fetchone()
    if not row:
        return flash_redirect(admin_url(request, "/storage"), "存储方式不存在。")
    try:
        if row["kind"] == "local":
            message = "本机存储可用。"
        else:
            message = build_backend(str(row["kind"]), _storage_config(row, config)).test()
    except (BackendError, StorageError, OSError, ValueError) as exc:
        return flash_redirect(
            admin_url(request, "/storage"), f"连接失败：{exc}"
        )
    audit(
        db,
        "storage.test",
        actor=admin["username"],
        target=str(backend_id),
        detail=str(row["name"]),
    )
    db.commit()
    return flash_redirect(admin_url(request, "/storage"), message)


@router.post("/storage/{backend_id}/delete")
def storage_delete(
    request: Request,
    backend_id: int,
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    storage: Storage = Depends(get_storage),
    admin: sqlite3.Row = Depends(require_admin),
):
    require_csrf(request, csrf, config)
    row = db.execute(
        "SELECT * FROM storage_backends WHERE id = ?", (backend_id,)
    ).fetchone()
    if not row:
        return flash_redirect(admin_url(request, "/storage"), "存储方式不存在。")
    if row["kind"] == "local":
        return flash_redirect(admin_url(request, "/storage"), "本机存储不能删除。")
    used = storage.backend_usage(db, backend_id)
    if used:
        return flash_redirect(
            admin_url(request, "/storage"),
            "该存储方式里还有图片，请先迁移或删除后再移除。",
        )
    db.execute("DELETE FROM storage_backends WHERE id = ?", (backend_id,))
    audit(
        db,
        "storage.delete",
        actor=admin["username"],
        target=str(backend_id),
        detail=str(row["name"]),
    )
    db.commit()
    storage.reload_backends()
    return flash_redirect(admin_url(request, "/storage"), "存储方式已移除。")


@router.get("/settings")
def settings_page(
    request: Request,
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    admin: sqlite3.Row = Depends(require_admin),
    geo: GeoResolver = Depends(get_geo),
):
    values = raw_settings(db)
    key_mode = str(values.get("guest_upload_key_mode", "off"))
    return render(
        request,
        db,
        "admin/settings.html",
        _admin_ctx(
            admin,
            values=values,
            albums=_album_choices(db),
            guest_daily_key=daily_key(config) if key_mode == "daily" else "",
            guest_key_configured=bool(
                values.get("guest_upload_key_hash")
                if key_mode == "fixed"
                else values.get("guest_upload_once_hash")
                if key_mode == "once"
                else True
            ),
            guest_once_code=values.get("guest_upload_once_code", ""),
            guest_once_used=values.get("guest_upload_once_used", "0") == "1",
            geo_status=geo.status(),
            active="settings",
        ),
    )


@router.post("/settings")
def settings_save(
    request: Request,
    site_name: str = Form("Ostensory"),
    site_subtitle: str = Form(""),
    site_description: str = Form(""),
    public_base_url: str = Form(""),
    experimental_features: str = Form(""),
    blur_nsfw: str = Form(""),
    nsfw_blocked_countries: str = Form(""),
    geoip_fail_mode: str = Form("block"),
    per_page: str = Form("48"),
    max_upload_mb: str = Form("200"),
    allowed_extensions: str = Form("png,jpg,jpeg,webp,gif"),
    client_download_enabled: str = Form(""),
    accent_color: str = Form("#d7a76c"),
    site_icon_url: str = Form(""),
    external_image_enabled: str = Form(""),
    cursor_style: str = Form("default"),
    cursor_custom_url: str = Form(""),
    cursor_hotspot_x: str = Form("4"),
    cursor_hotspot_y: str = Form("4"),
    browse_mode: str = Form("pagination"),
    browse_layout: str = Form("masonry"),
    albums_layout: str = Form("rows"),
    stats_enabled: str = Form(""),
    stats_show_counts: str = Form(""),
    stats_show_storage: str = Form(""),
    stats_show_metadata: str = Form(""),
    stats_show_activity: str = Form(""),
    stats_show_albums: str = Form(""),
    stats_show_guest: str = Form(""),
    guest_upload_enabled: str = Form(""),
    guest_upload_log_ip: str = Form(""),
    guest_upload_key_mode: str = Form("off"),
    guest_upload_fixed_key: str = Form(""),
    guest_upload_album_ids: list[str] = Form([]),
    guest_upload_max_mb: str = Form("20"),
    guest_upload_quota_mb: str = Form("1024"),
    guest_upload_cycle_enabled: str = Form(""),
    guest_upload_cycle_scope: str = Form("album"),
    guest_upload_interval_seconds: str = Form("30"),
    guest_upload_max_files: str = Form("20"),
    geoip_public_update_enabled: str = Form(""),
    home_mode: str = Form("waterfall"),
    home_layout: str = Form("masonry"),
    home_carousel_kind: str = Form("assets"),
    home_carousel_ids: str = Form(""),
    home_carousel_interval: str = Form("6"),
    home_waterfall_source: str = Form("latest"),
    home_selected_ids: str = Form(""),
    footer_icp: str = Form(""),
    footer_icp_url: str = Form("https://beian.miit.gov.cn/"),
    footer_html: str = Form(""),
    custom_css: str = Form(""),
    custom_head_html: str = Form(""),
    about_enabled: str = Form(""),
    about_name: str = Form(""),
    about_avatar: str = Form(""),
    about_bio: str = Form(""),
    about_html: str = Form(""),
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    admin: sqlite3.Row = Depends(require_admin),
):
    require_csrf(request, csrf, config)
    base = public_base_url.strip().rstrip("/")
    if base and not re.match(r"^https?://[^\s]+$", base):
        return flash_redirect(admin_url(request, "/settings"), "对外地址必须是 http(s):// 开头的 URL。")
    extensions = sorted(
        {
            item.strip().lower().lstrip(".")
            for item in allowed_extensions.split(",")
            if item.strip()
        }
    )
    if not extensions:
        return flash_redirect(admin_url(request, "/settings"), "允许的扩展名不能为空。")
    if not (set(extensions) & {"png", "jpg", "jpeg", "webp", "gif"}):
        return flash_redirect(admin_url(request, "/settings"), "至少保留一种支持的图片格式。")
    accent = accent_color.strip()
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", accent):
        accent = "#d7a76c"
    mode = home_mode if home_mode in {"waterfall", "carousel", "carousel_waterfall"} else "waterfall"
    carousel_kind = home_carousel_kind if home_carousel_kind in {"assets", "albums"} else "assets"
    waterfall = (
        home_waterfall_source
        if home_waterfall_source in {"latest", "selected", "all"}
        else "latest"
    )
    album_layout = (
        albums_layout if albums_layout in {"rows", "cards", "compact"} else "rows"
    )
    browse_layout_mode = browse_layout if browse_layout in {"grid", "masonry"} else "masonry"
    home_layout_mode = home_layout if home_layout in {"masonry", "grid"} else "masonry"
    cursor_mode = (
        cursor_style
        if cursor_style in {"default", "ink", "ring", "cross", "diamond", "custom"}
        else "default"
    )
    custom_cursor = cursor_custom_url.strip()[:1000]
    cursor_warning = ""
    if cursor_mode == "custom" and not CURSOR_URL_RE.fullmatch(custom_cursor):
        cursor_mode = "default"
        custom_cursor = ""
        cursor_warning = "自定义鼠标地址无效，已恢复系统默认光标。"
    key_mode = (
        guest_upload_key_mode
        if guest_upload_key_mode in {"off", "fixed", "daily", "once"}
        else "off"
    )
    fixed_key = guest_upload_fixed_key.strip()
    if key_mode == "fixed" and not fixed_key and not get_setting(
        db, "guest_upload_key_hash", ""
    ):
        return flash_redirect(
            admin_url(request, "/settings"),
            "固定密钥模式需要设置一个密钥。",
        )
    key_warning = (
        "访客上传密钥较短，建议至少 6 位；已保存。"
        if key_mode == "fixed" and fixed_key and len(fixed_key) < 6
        else None
    )
    experimental = experimental_features == "1"
    allowed_guest_albums = _normalize_ids(",".join(guest_upload_album_ids))
    values = {
        "site_name": site_name.strip()[:80] or "Ostensory",
        "site_subtitle": site_subtitle.strip()[:160],
        "site_description": site_description.strip()[:500],
        "site_icon_url": site_icon_url.strip()[:500],
        "accent_color": accent,
        "public_base_url": base,
        "external_image_enabled": "1" if external_image_enabled == "1" else "0",
        "cursor_style": cursor_mode,
        "cursor_custom_url": custom_cursor,
        "cursor_hotspot_x": str(parse_int(cursor_hotspot_x, 4, 0, 64)),
        "cursor_hotspot_y": str(parse_int(cursor_hotspot_y, 4, 0, 64)),
        "experimental_features": "1" if experimental else "0",
        "blur_nsfw": "1" if blur_nsfw == "1" else "0",
        "nsfw_blocked_countries": (
            countries_json(nsfw_blocked_countries)
            if experimental
            else get_setting(db, "nsfw_blocked_countries", '["CN"]')
        ),
        "geoip_fail_mode": (
            "allow" if geoip_fail_mode == "allow" else "block"
        ) if experimental else get_setting(db, "geoip_fail_mode", "block"),
        "per_page": str(parse_int(per_page, 48, 6, 200)),
        "max_upload_mb": str(parse_int(max_upload_mb, 200, 1, 2048)),
        "allowed_extensions": ",".join(extensions),
        "client_download_enabled": "1" if client_download_enabled == "1" else "0",
        "browse_mode": browse_mode if browse_mode in {"pagination", "infinite"} else "pagination",
        "browse_layout": browse_layout_mode,
        "albums_layout": album_layout,
        "stats_enabled": "1" if stats_enabled == "1" else "0",
        "stats_show_counts": "1" if stats_show_counts == "1" else "0",
        "stats_show_storage": "1" if stats_show_storage == "1" else "0",
        "stats_show_metadata": "1" if stats_show_metadata == "1" else "0",
        "stats_show_activity": "1" if stats_show_activity == "1" else "0",
        "stats_show_albums": "1" if stats_show_albums == "1" else "0",
        "stats_show_guest": "1" if stats_show_guest == "1" else "0",
        "guest_upload_enabled": "1" if guest_upload_enabled == "1" else "0",
        "guest_upload_log_ip": "1" if guest_upload_log_ip == "1" else "0",
        "guest_upload_key_mode": key_mode,
        "guest_upload_album_ids": allowed_guest_albums,
        "guest_upload_max_mb": str(parse_int(guest_upload_max_mb, 20, 1, 2048)),
        "guest_upload_quota_mb": str(
            parse_int(guest_upload_quota_mb, 1024, 0, 1024 * 1024)
        ),
        "guest_upload_cycle_enabled": "1" if guest_upload_cycle_enabled == "1" else "0",
        "guest_upload_cycle_scope": (
            guest_upload_cycle_scope
            if guest_upload_cycle_scope in {"album", "all"}
            else "album"
        ),
        "guest_upload_interval_seconds": str(
            parse_int(guest_upload_interval_seconds, 30, 0, 86400)
        ),
        "guest_upload_max_files": str(parse_int(guest_upload_max_files, 20, 1, 100)),
        "geoip_public_update_enabled": (
            "1" if experimental and geoip_public_update_enabled == "1" else "0"
        ),
        "home_mode": mode,
        "home_layout": home_layout_mode,
        "home_carousel_kind": carousel_kind,
        "home_carousel_ids": _normalize_ids(home_carousel_ids),
        "home_carousel_interval": str(parse_int(home_carousel_interval, 6, 2, 30)),
        "home_waterfall_source": waterfall,
        "home_selected_ids": _normalize_ids(home_selected_ids),
        "footer_icp": footer_icp.strip()[:160],
        "footer_icp_url": footer_icp_url.strip()[:500],
        "footer_html": footer_html.strip()[:30000],
        "custom_css": custom_css.strip()[:80000],
        "custom_head_html": custom_head_html.strip()[:30000],
        "about_enabled": "1" if about_enabled == "1" else "0",
        "about_name": about_name.strip()[:120],
        "about_avatar": about_avatar.strip()[:500],
        "about_bio": about_bio.strip()[:5000],
        "about_html": about_html.strip()[:80000],
    }
    for key, value in values.items():
        set_setting(db, key, value)
    if fixed_key:
        set_fixed_key(db, config, fixed_key)
    audit(
        db,
        "settings.update",
        actor=admin["username"],
        detail=",".join(values.keys()),
        ip=getattr(request.state, "client_ip", ""),
    )
    db.commit()
    message = "设置已保存。"
    if key_warning:
        message += " " + key_warning
    if cursor_warning:
        message += " " + cursor_warning
    return flash_redirect(admin_url(request, "/settings"), message)


@router.post("/experimental/reset")
def experimental_reset(
    request: Request,
    confirm_1: str = Form(""),
    confirm_2: str = Form(""),
    confirm_phrase: str = Form(""),
    csrf: str = Form(""),
    config: Config = Depends(get_config),
):
    require_csrf(request, csrf, config)

    # Authenticate with a short-lived connection.  This route deliberately does
    # not use the normal DB dependency: the reset deletes the database and all
    # data files, and a dependency-held connection would block that on Windows.
    conn = connect(config.db_path)
    try:
        admin = current_admin(conn, request)
        if not admin:
            return redirect(admin_url(request, "/login"))
        if get_setting(conn, "experimental_features", "0") != "1":
            raise HTTPException(status_code=404, detail="实验性功能未开启")
    finally:
        conn.close()

    if (
        confirm_1 != "1"
        or confirm_2 != "1"
        or confirm_phrase.strip() != "完全重置"
    ):
        return flash_redirect(
            admin_url(request, "/settings"),
            "重置未执行：必须完成全部三次确认。",
        )

    reset_application_data(request.app, config)
    response = RedirectResponse(
        "/admin/setup?msg=" + quote("应用已完全重置，请重新初始化。"),
        status_code=303,
    )
    clear_session_cookie(response)
    clear_unlock_cookie(response)
    response.delete_cookie("cb_csrf", path="/")
    return response


@router.post("/settings/guest-key")
def settings_guest_key(
    request: Request,
    action: str = Form("generate_once"),
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    admin: sqlite3.Row = Depends(require_admin),
):
    require_csrf(request, csrf, config)
    if action == "generate_once":
        code = generate_once_code(db, config)
        db.commit()
        audit(
            db,
            "settings.guest_key",
            actor=admin["username"],
            detail="generate_once",
            ip=getattr(request.state, "client_ip", ""),
        )
        db.commit()
        return flash_redirect(
            admin_url(request, "/settings"),
            f"已生成一次性密钥：{code}（只显示在后台，请及时发给访客）",
        )
    if action == "reset_once":
        set_setting(db, "guest_upload_once_used", "0")
        db.commit()
        return flash_redirect(admin_url(request, "/settings"), "一次性密钥已重新启用。")
    return flash_redirect(admin_url(request, "/settings"), "未知的密钥操作。")


@router.post("/settings/branding")
async def settings_branding(
    request: Request,
    kind: str = Form(""),
    file: UploadFile = File(...),
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    admin: sqlite3.Row = Depends(require_admin),
):
    require_csrf(request, csrf, config)
    if kind not in {"icon", "avatar"}:
        return flash_redirect(admin_url(request, "/settings"), "未知的上传类型。")
    name = (file.filename or "").strip()
    suffix = Path(name).suffix.lower()
    allowed = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".svg"}
    if suffix not in allowed:
        return flash_redirect(admin_url(request, "/settings"), "图标只支持 PNG/JPG/WEBP/GIF/ICO/SVG。")
    data = await file.read()
    if not data:
        return flash_redirect(admin_url(request, "/settings"), "文件为空。")
    if len(data) > 5 * 1024 * 1024:
        return flash_redirect(admin_url(request, "/settings"), "图标不能超过 5 MB。")
    if suffix != ".svg":
        try:
            Image.open(io.BytesIO(data)).verify()
        except Exception:
            return flash_redirect(admin_url(request, "/settings"), "这个文件不是可识别的图片。")
    else:
        head = data[:512].lower()
        if b"<svg" not in head:
            return flash_redirect(admin_url(request, "/settings"), "SVG 内容无法识别。")
    branding = Path(config.data_dir) / "branding"
    branding.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()[:16]
    target_name = f"{kind}-{digest}{suffix}"
    target = branding / target_name
    target.write_bytes(data)
    try:
        target.chmod(0o600)
    except OSError:
        pass
    key = "site_icon_url" if kind == "icon" else "about_avatar"
    set_setting(db, key, f"/branding/{target_name}")
    audit(db, f"settings.{key}", actor=admin["username"], target=target_name)
    db.commit()
    return flash_redirect(admin_url(request, "/settings"), "图标已更新。")


@router.post("/settings/admin-path")
def settings_admin_path(
    request: Request,
    admin_path: str = Form(""),
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    admin: sqlite3.Row = Depends(require_admin),
):
    require_csrf(request, csrf, config)
    value = admin_path.strip().strip("/")
    reserved = value.lower() in RESERVED_ADMIN_PATHS and value.lower() != "admin"
    if not ADMIN_PATH_RE.match(value) or reserved:
        return flash_redirect(
            admin_url(request, "/settings"),
            "后台路径需为 3-32 位字母、数字、下划线或连字符，且不能使用系统保留名称。",
        )
    old = request.app.state.admin_path
    set_setting(db, "admin_path", value)
    db.commit()
    request.app.state.admin_path = value
    audit(db, "settings.admin_path", actor=admin["username"], detail=f"{old} -> {value}")
    return flash_redirect(f"/{value}/settings", f"后台路径已改为 /{value}。")


@router.get("/account")
def account_page(
    request: Request,
    db: sqlite3.Connection = Depends(get_db_conn),
    admin: sqlite3.Row = Depends(require_admin),
):
    return render(
        request,
        db,
        "admin/account.html",
        _admin_ctx(admin, active="account"),
    )


@router.post("/account")
def account_save(
    request: Request,
    current_password: str = Form(...),
    username: str = Form(""),
    new_password: str = Form(""),
    new_password2: str = Form(""),
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    admin: sqlite3.Row = Depends(require_admin),
):
    require_csrf(request, csrf, config)
    if not verify_password(current_password, admin["password_hash"]):
        return flash_redirect(admin_url(request, "/account"), "当前密码不正确。")
    new_username = username.strip() or admin["username"]
    if not USERNAME_RE.match(new_username):
        return flash_redirect(admin_url(request, "/account"), "用户名格式不正确。")
    password_warning_text = None
    if new_password:
        password_warning_text = password_warning(new_password)
        if new_password != new_password2:
            return flash_redirect(admin_url(request, "/account"), "两次输入的密码不一致。")
    try:
        db.execute(
            "UPDATE users SET username = ?, password_hash = ?, updated_at = ? WHERE id = ?",
            (
                new_username,
                hash_password(new_password) if new_password else admin["password_hash"],
                utc_now(),
                admin["id"],
            ),
        )
        db.commit()
    except sqlite3.IntegrityError:
        return flash_redirect(admin_url(request, "/account"), "用户名已被占用。")
    if new_password:
        import hashlib

        current = current_session_token(request) or ""
        db.execute(
            "DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
            (admin["id"], hashlib.sha256(current.encode()).hexdigest()),
        )
        db.commit()
    audit(db, "account.update", actor=new_username)
    message = "账户已更新。"
    if password_warning_text:
        message += " " + password_warning_text
    return flash_redirect(admin_url(request, "/account"), message)


@router.get("/tokens")
def tokens_page(
    request: Request,
    db: sqlite3.Connection = Depends(get_db_conn),
    admin: sqlite3.Row = Depends(require_admin),
):
    tokens = db.execute("SELECT * FROM api_tokens ORDER BY id DESC").fetchall()
    return render(
        request,
        db,
        "admin/tokens.html",
        _admin_ctx(admin, tokens=tokens, active="tokens"),
    )


@router.post("/tokens")
def token_create(
    request: Request,
    name: str = Form("我的客户端"),
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    admin: sqlite3.Row = Depends(require_admin),
):
    require_csrf(request, csrf, config)
    from .security import create_api_token

    token = create_api_token(db, admin["id"], name)
    db.commit()
    tokens = db.execute("SELECT * FROM api_tokens ORDER BY id DESC").fetchall()
    audit(db, "api_token.create", actor=admin["username"], detail=name)
    return render(
        request,
        db,
        "admin/tokens.html",
        _admin_ctx(admin, tokens=tokens, new_token=token, active="tokens"),
    )


@router.post("/tokens/{token_id}/revoke")
def token_revoke(
    token_id: int,
    request: Request,
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    admin: sqlite3.Row = Depends(require_admin),
):
    require_csrf(request, csrf, config)
    db.execute(
        "UPDATE api_tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
        (utc_now(), token_id),
    )
    audit(db, "api_token.revoke", actor=admin["username"], target=str(token_id))
    db.commit()
    return flash_redirect(admin_url(request, "/tokens"), "Token 已撤销。")


@router.post("/geoip/update")
def geoip_update(
    request: Request,
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    admin: sqlite3.Row = Depends(require_admin),
):
    require_csrf(request, csrf, config)
    _require_experimental(db)
    if get_setting(db, "geoip_public_update_enabled", "0") != "1":
        return flash_redirect(
            admin_url(request, "/geoip"),
            "请先在设置页开启“允许从 DB-IP Lite 拉取公开国家数据库”。",
        )
    old = getattr(request.app.state, "geo", None)
    # Windows keeps an open MMDB handle locked; close it before the atomic
    # replace, then reload either the new file or the previous one on failure.
    if old is not None:
        old.close()
    try:
        ok, message = download_public_database(config)
    finally:
        request.app.state.geo = GeoResolver(config)
    if ok:
        audit(
            db,
            "geoip.update",
            actor=admin["username"],
            detail=message,
            ip=getattr(request.state, "client_ip", ""),
        )
        db.commit()
    return flash_redirect(admin_url(request, "/geoip"), message)


@router.get("/geoip")
def geoip_page(
    request: Request,
    test_ip: str = "",
    db: sqlite3.Connection = Depends(get_db_conn),
    admin: sqlite3.Row = Depends(require_admin),
    geo: GeoResolver = Depends(get_geo),
):
    _require_experimental(db)
    status = geo.status()
    current_country, current_source, current_ip = geo.country_for_request(request, request.app.state.config)
    lookup = None
    if test_ip.strip():
        country, source = geo.lookup(test_ip.strip())
        lookup = {"ip": test_ip.strip(), "country": country, "source": source}
    return render(
        request,
        db,
        "admin/geoip.html",
        _admin_ctx(
            admin,
            status=status,
            current_ip=current_ip,
            current_country=current_country,
            current_source=current_source,
            lookup=lookup,
            test_ip=test_ip,
            public_update_enabled=(
                get_setting(db, "geoip_public_update_enabled", "0") == "1"
            ),
            active="geoip",
        ),
    )