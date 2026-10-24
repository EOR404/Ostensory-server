from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from .access import (
    asset_query,
    descendant_album_ids,
    ensure_album_path,
    parse_countries,
)
from .config import Config
from .database import audit, get_setting, utc_now
from .deps import get_config, get_db_conn, get_storage, require_api_user
from .security import (
    clear_login_throttle,
    cooldown_text,
    create_api_token,
    login_retry_after,
    parse_basic_auth,
    register_login_failure,
    verify_password,
)
from .services import (
    create_asset_from_upload,
    safe_display_name,
)
from .storage import Storage, StorageError, StorageFull

router = APIRouter(prefix="/api/v1")
SHA_RE = re.compile(r"^[a-fA-F0-9]{64}$")


def _sync_visible_asset_sql() -> str:
    """Assets an API token owner may sync.

    Public approved images plus the caller's own approved/private or pending
    images.  Rejected images never enter a sync manifest or download route.
    """
    return (
        "((a.status = 'approved' AND (a.visibility = 'public' OR a.owner_id = ?)) "
        "OR (a.owner_id = ? AND a.status = 'pending'))"
    )


@router.get("/health")
def health(db: sqlite3.Connection = Depends(get_db_conn), config: Config = Depends(get_config)):
    users = db.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    return {
        "ok": True,
        "service": "Ostensory",
        "version": config.version,
        "setup": int(users) > 0,
        "time": utc_now(),
    }


@router.post("/auth/token")
async def auth_token(
    request: Request,
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
):
    body: dict = {}
    try:
        raw = await request.body()
        if raw:
            parsed = json.loads(raw.decode("utf-8"))
            if isinstance(parsed, dict):
                body = parsed
    except (ValueError, UnicodeDecodeError):
        body = {}
    basic = parse_basic_auth(request.headers.get("authorization"))
    if basic:
        username, password = basic
    else:
        username = str(body.get("username", ""))
        password = str(body.get("password", ""))
    name = str(body.get("name") or request.headers.get("x-client-name") or "Ostensory 客户端")
    ip = getattr(request.state, "client_ip", "") or (request.client.host if request.client else "")
    retry_after = login_retry_after(db, ip)
    if retry_after:
        raise HTTPException(
            status_code=429,
            detail=f"登录尝试过于频繁，请在 {cooldown_text(retry_after)} 后再试",
            headers={"Retry-After": str(retry_after)},
        )
    user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if not user or not verify_password(password, user["password_hash"]):
        delay = register_login_failure(db, ip)
        if delay:
            raise HTTPException(
                status_code=429,
                detail=f"登录尝试过于频繁，请在 {cooldown_text(delay)} 后再试",
                headers={"Retry-After": str(delay)},
            )
        raise HTTPException(status_code=401, detail="用户名或密码不正确")
    clear_login_throttle(db, ip)
    token = create_api_token(db, user["id"], name)
    audit(db, "api_token.create", actor=username, target=str(user["id"]), ip=ip)
    db.commit()
    return {
        "token": token,
        "token_id": db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"],
        "user": username,
        "note": "请立即保存 Token，它只会显示这一次。",
    }


@router.get("/me")
def me(user: sqlite3.Row = Depends(require_api_user)):
    return {
        "user": user["username"],
        "username": user["username"],
        "token_id": user["id"],
        "token_name": user["name"],
        "last_used_at": user["last_used_at"],
    }


@router.post("/sync/ensure-album")
async def sync_ensure_album(
    request: Request,
    db: sqlite3.Connection = Depends(get_db_conn),
    user: sqlite3.Row = Depends(require_api_user),
):
    body = {}
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="需要 JSON 请求体")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="请求体格式错误")
    path = str(body.get("path", "")).strip()
    if not path:
        raise HTTPException(status_code=400, detail="path 不能为空")
    try:
        album = ensure_album_path(db, path, create=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not album:
        raise HTTPException(status_code=400, detail="无法创建相册")
    db.commit()
    audit(db, "sync.ensure_album", actor=user["username"], target=album["path"])
    return {
        "id": album["id"],
        "path": album["path"],
        "name": album["name"],
        "parent_id": album["parent_id"],
    }


def _clean_client_path(value: str) -> str:
    value = (value or "").replace("\\", "/").strip()
    if not value or value.startswith("/"):
        raise ValueError("relative_path 必须是非空相对路径")
    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("relative_path 包含非法路径片段")
    if len(value) > 1000 or "\x00" in value:
        raise ValueError("relative_path 过长或包含非法字符")
    return path.as_posix()


@router.post("/sync/upload")
def sync_upload(
    request: Request,
    file: UploadFile = File(...),
    album_id: int = Form(...),
    relative_path: str = Form(...),
    sha256: str = Form(""),
    mtime: float = Form(0.0),
    overwrite: bool = Form(True),
    metadata_json: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    storage: Storage = Depends(get_storage),
    user: sqlite3.Row = Depends(require_api_user),
):
    _ = mtime
    try:
        client_path = _clean_client_path(relative_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    expected = sha256.strip().lower()
    if expected and not SHA_RE.match(expected):
        raise HTTPException(status_code=400, detail="sha256 格式错误")
    album = db.execute("SELECT * FROM albums WHERE id = ?", (album_id,)).fetchone()
    if not album:
        raise HTTPException(status_code=404, detail="相册不存在")
    if not file.filename:
        raise HTTPException(status_code=400, detail="缺少文件名")
    client_metadata = None
    if metadata_json.strip():
        if len(metadata_json.encode("utf-8")) > 4 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="客户端元数据超过 4 MB")
        try:
            parsed_metadata = json.loads(metadata_json)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="客户端元数据不是有效 JSON") from exc
        if not isinstance(parsed_metadata, dict):
            raise HTTPException(status_code=400, detail="客户端元数据必须是 JSON 对象")
        client_metadata = parsed_metadata
    try:
        asset, status = create_asset_from_upload(
            db,
            storage,
            file.file,
            file.filename,
            album_id,
            source_name="client",
            client_path=client_path,
            overwrite=overwrite,
            expected_sha256=expected or None,
            metadata=client_metadata,
            owner_id=int(user["account_id"]),
            author_name=(
                str(user["display_name"] or "").strip()
                or ("管理员" if user["role"] == "admin" else str(user["username"]))
            ),
        )
    except StorageFull as exc:
        raise HTTPException(status_code=507, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    metadata = {}
    try:
        metadata = json.loads(asset["metadata_json"] or "{}")
    except ValueError:
        pass
    audit(
        db,
        "sync.upload",
        actor=user["username"],
        target=str(asset["id"]),
        detail=client_path,
        ip=getattr(request.state, "client_ip", ""),
    )
    return JSONResponse(
        {
            "status": status,
            "asset_id": asset["id"],
            "album_id": album_id,
            "sha256": asset["sha256"],
            "path": client_path,
            "url": f"/images/{asset['id']}",
            "metadata": {
                "kind": metadata.get("kind", "unknown"),
                "positive_prompt": str(metadata.get("positive_prompt", ""))[:2000],
                "lora_count": len(metadata.get("loras") or []),
            },
        }
    )


@router.post("/sync/check-duplicates")
def sync_check_duplicates(
    payload: dict = Body(...),
    db: sqlite3.Connection = Depends(get_db_conn),
    user: sqlite3.Row = Depends(require_api_user),
):
    values = payload.get("sha256")
    if not isinstance(values, list):
        raise HTTPException(status_code=400, detail="sha256 必须是数组")
    hashes: list[str] = []
    for value in values[:1000]:
        text = str(value or "").strip().lower()
        if SHA_RE.fullmatch(text) and text not in hashes:
            hashes.append(text)
    if not hashes:
        return {"duplicates": []}
    placeholders = ",".join("?" for _ in hashes)
    rows = db.execute(
        "SELECT DISTINCT lower(b.sha256) AS sha256 FROM assets a "
        "JOIN blobs b ON b.id = a.blob_id "
        f"WHERE a.deleted_at IS NULL AND {_sync_visible_asset_sql()} "
        f"AND lower(b.sha256) IN ({placeholders})",
        (int(user["id"]), int(user["id"]), *hashes),
    ).fetchall()
    return {"duplicates": sorted(str(row["sha256"]) for row in rows)}


@router.get("/sync/manifest")
def sync_manifest(
    album_id: int,
    include_descendants: bool = True,
    db: sqlite3.Connection = Depends(get_db_conn),
    user: sqlite3.Row = Depends(require_api_user),
):
    album = db.execute("SELECT * FROM albums WHERE id = ?", (album_id,)).fetchone()
    if not album:
        raise HTTPException(status_code=404, detail="相册不存在")
    ids = descendant_album_ids(db, album_id) if include_descendants else [album_id]
    placeholders = ",".join("?" for _ in ids)
    rows = db.execute(
        asset_query()
        + f"WHERE a.album_id IN ({placeholders}) AND a.deleted_at IS NULL "
        f"AND {_sync_visible_asset_sql()} "
        "AND a.client_path IS NOT NULL AND a.client_path != '' ORDER BY a.client_path",
        (*ids, int(user["id"]), int(user["id"])),
    ).fetchall()
    download_enabled = str(get_setting(db, "client_download_enabled", "1")).lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    files = []
    for row in rows:
        files.append(
            {
                "asset_id": row["id"],
                "path": row["client_path"],
                "sha256": row["sha256"],
                "size": row["size"],
                "mtime": row["created_at"],
                "download_url": f"/api/v1/sync/download/{row['id']}" if download_enabled else None,
            }
        )
    return {
        "album_id": album_id,
        "album_path": album["path"],
        "download_enabled": download_enabled,
        "files": files,
    }


@router.get("/sync/download/{asset_id}")
def sync_download(
    asset_id: int,
    db: sqlite3.Connection = Depends(get_db_conn),
    storage: Storage = Depends(get_storage),
    user: sqlite3.Row = Depends(require_api_user),
):
    if str(get_setting(db, "client_download_enabled", "1")).lower() not in {"1", "true", "yes", "on"}:
        raise HTTPException(status_code=403, detail="管理员已关闭客户端下载同步")
    asset = db.execute(
        asset_query()
        + "WHERE a.id = ? AND a.deleted_at IS NULL AND "
        + _sync_visible_asset_sql(),
        (asset_id, int(user["id"]), int(user["id"])),
    ).fetchone()
    if not asset:
        raise HTTPException(status_code=404, detail="图片不存在")
    path = storage.path_for(asset["storage_key"])
    if not path or not path.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
    if asset["encrypted"] or storage.is_encrypted_file(path):
        data = storage.decrypt_to_bytes(path, True)
        headers["Content-Disposition"] = (
            f"attachment; filename*=UTF-8''{quote(safe_display_name(asset['original_name']))}"
        )
        return Response(content=data, media_type=asset["mime"], headers=headers)
    return FileResponse(
        path,
        media_type=asset["mime"],
        filename=safe_display_name(asset["original_name"]),
        content_disposition_type="attachment",
        headers=headers,
    )


@router.get("/sync/albums")
def sync_albums(
    db: sqlite3.Connection = Depends(get_db_conn),
    user: sqlite3.Row = Depends(require_api_user),
):
    rows = db.execute("SELECT * FROM albums ORDER BY path COLLATE NOCASE").fetchall()
    result = []
    for row in rows:
        count = db.execute(
            "SELECT COUNT(*) AS n FROM assets a WHERE a.album_id = ? "
            "AND a.deleted_at IS NULL AND " + _sync_visible_asset_sql(),
            (row["id"], int(user["id"]), int(user["id"])),
        ).fetchone()["n"]
        result.append(
            {
                "id": row["id"],
                "path": row["path"],
                "name": row["name"],
                "parent_id": row["parent_id"],
                "asset_count": count,
            }
        )
    return {"albums": result}


@router.get("/sync/ping")
def sync_ping(user: sqlite3.Row = Depends(require_api_user)):
    return {"ok": True, "user": user["username"]}