from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from urllib.parse import quote, urlencode
from urllib.request import Request as UrlRequest, urlopen

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from .access import (
    asset_query,
    check_album,
    countries_json,
    validate_region_mode,
)
from .config import Config
from .database import audit, get_setting, utc_now
from .deps import get_config, get_db_conn, get_geo, get_storage, require_csrf
from .geo import GeoResolver
from .guest_upload import album_choices
from .quotas import default_quota_mb
from .security import (
    auth_limiter,
    clear_login_throttle,
    clear_session_cookie,
    cooldown_text,
    create_session,
    current_session_token,
    current_user,
    delete_session,
    hash_password,
    login_retry_after,
    password_warning,
    register_login_failure,
    set_session_cookie,
    verify_password,
)
from .services import delete_asset
from .storage import Storage
from .trash import expiry_for, rejected_retention_days
from .users import USERNAME_RE, user_display_name
from .web import (
    INTRO_COLOR_RE,
    INTRO_STYLE_MAP,
    admin_url,
    flash_redirect,
    parse_int,
    redirect,
    render,
    safe_next,
    site_settings,
)

router = APIRouter()


def _ip(request: Request) -> str:
    return getattr(request.state, "client_ip", "") or (
        request.client.host if request.client else ""
    )


def _provider_state(db: sqlite3.Connection) -> dict:
    settings = site_settings(db)
    password_on = settings.get("auth_password_enabled", "1") == "1"
    qq_on = settings.get("auth_qq_enabled", "0") == "1"
    discord_on = settings.get("auth_discord_enabled", "0") == "1"
    qq_ready = bool(settings.get("qq_client_id") and settings.get("qq_client_secret"))
    discord_ready = bool(
        settings.get("discord_client_id") and settings.get("discord_client_secret")
    )
    return {
        "password_enabled": password_on,
        "register_enabled": settings.get("registration_enabled", "0") == "1",
        "qq_enabled": qq_on and qq_ready,
        "discord_enabled": discord_on and discord_ready,
        "any_login": password_on or (qq_on and qq_ready) or (discord_on and discord_ready),
    }


def _sign_state(payload: str, config: Config) -> str:
    secret = (
        config.secret_key
        if isinstance(config.secret_key, bytes)
        else str(config.secret_key).encode("utf-8")
    )
    return hmac.new(secret, f"oauth:{payload}".encode("utf-8"), hashlib.sha256).hexdigest()


def _make_state(provider: str, next_url: str, config: Config) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps(
            {"p": provider, "n": next_url, "t": int(time.time()), "r": secrets.token_urlsafe(12)}
        ).encode("utf-8")
    ).decode("ascii")
    return f"{payload}.{_sign_state(payload, config)}"


def _read_state(state: str, provider: str, config: Config) -> str | None:
    if "." not in state:
        return None
    payload, signature = state.rsplit(".", 1)
    if not hmac.compare_digest(signature, _sign_state(payload, config)):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (ValueError, json.JSONDecodeError):
        return None
    if data.get("p") != provider or int(data.get("t", 0)) < int(time.time()) - 600:
        return None
    return safe_next(str(data.get("n") or ""), "/account")


def _callback_url(request: Request, db: sqlite3.Connection, provider: str) -> str:
    base = (site_settings(db).get("public_base_url") or "").rstrip("/")
    if not base:
        base = str(request.base_url).rstrip("/")
    return f"{base}/auth/{provider}/callback"


def _json_request(url: str, data: dict | None = None, headers: dict | None = None) -> dict:
    body = urlencode(data).encode("utf-8") if data is not None else None
    req = UrlRequest(
        url,
        data=body,
        headers={
            "Accept": "application/json",
            "User-Agent": "Ostensory/1.0",
            **(headers or {}),
        },
        method="POST" if body is not None else "GET",
    )
    with urlopen(req, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def _oauth_profile(provider: str, code: str, redirect_uri: str, db: sqlite3.Connection) -> tuple[str, str]:
    settings = site_settings(db)
    if provider == "qq":
        token = _json_request(
            "https://graph.qq.com/oauth2.0/token?"
            + urlencode(
                {
                    "grant_type": "authorization_code",
                    "client_id": settings.get("qq_client_id", ""),
                    "client_secret": settings.get("qq_client_secret", ""),
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "fmt": "json",
                }
            )
        )
        access_token = token.get("access_token")
        if not access_token:
            raise ValueError("QQ 没有返回访问令牌。")
        me = _json_request(
            "https://graph.qq.com/oauth2.0/me?"
            + urlencode({"access_token": access_token, "fmt": "json"})
        )
        openid = me.get("openid")
        if not openid:
            raise ValueError("QQ 没有返回 openid。")
        info = _json_request(
            "https://graph.qq.com/user/get_user_info?"
            + urlencode(
                {
                    "access_token": access_token,
                    "oauth_consumer_key": settings.get("qq_client_id", ""),
                    "openid": openid,
                }
            )
        )
        nickname = str(info.get("nickname") or "").strip()
        return str(openid), nickname
    token = _json_request(
        "https://discord.com/api/oauth2/token",
        data={
            "client_id": settings.get("discord_client_id", ""),
            "client_secret": settings.get("discord_client_secret", ""),
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    access_token = token.get("access_token")
    if not access_token:
        raise ValueError("Discord 没有返回访问令牌。")
    profile = _json_request(
        "https://discord.com/api/users/@me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    user_id = profile.get("id")
    if not user_id:
        raise ValueError("Discord 没有返回用户 ID。")
    nickname = str(profile.get("global_name") or profile.get("username") or "").strip()
    return str(user_id), nickname


def _unique_username(db: sqlite3.Connection, base: str) -> str:
    cleaned = "".join(
        ch for ch in base if ch.isascii() and (ch.isalnum() or ch in "_.@-")
    )[:48]
    if len(cleaned) < 3:
        cleaned = "user"
    candidate = cleaned
    suffix = 1
    while db.execute(
        "SELECT 1 FROM users WHERE username = ? COLLATE NOCASE", (candidate,)
    ).fetchone():
        suffix += 1
        candidate = f"{cleaned[:54]}_{suffix}"
    return candidate


def _oauth_login(
    db: sqlite3.Connection,
    request: Request,
    config: Config,
    provider: str,
    subject: str,
    nickname: str,
    next_url: str,
    storage: Storage | None = None,
):
    user = db.execute(
        "SELECT * FROM users WHERE oauth_provider = ? AND oauth_subject = ?",
        (provider, subject),
    ).fetchone()
    if user and not int(user["is_active"]):
        return redirect("/login?error=" + quote("这个账号已被停用。"))
    now = utc_now()
    if not user:
        username = _unique_username(
            db, f"{provider}_{nickname}" if nickname else f"{provider}_{subject}"
        )
        cursor = db.execute(
            "INSERT INTO users(username, password_hash, role, display_name, is_active, "
            "quota_mb, oauth_provider, oauth_subject, created_at, updated_at, last_login_at) "
            "VALUES(?,?,?,?,1,?,?,?,?,?,?)",
            (
                username,
                hash_password(secrets.token_urlsafe(32)),
                "user",
                nickname[:40],
                default_quota_mb(db, storage),
                provider,
                subject,
                now,
                now,
                now,
            ),
        )
        user_id = int(cursor.lastrowid)
        audit(db, f"{provider}.register", actor=username, target=str(user_id))
    else:
        user_id = int(user["id"])
        db.execute(
            "UPDATE users SET last_login_at = ?, display_name = CASE WHEN display_name = '' "
            "THEN ? ELSE display_name END, updated_at = ? WHERE id = ?",
            (now, nickname[:40], now, user_id),
        )
    token, _ = create_session(db, user_id, request, config)
    audit(db, f"{provider}.login", actor=str(user_id))
    db.commit()
    response = RedirectResponse(next_url or "/account", status_code=303)
    response.delete_cookie("cb_oauth_state", path="/")
    set_session_cookie(response, request, config, token)
    return response


@router.get("/auth/{provider}/start")
def oauth_start(
    provider: str,
    request: Request,
    next: str = "",
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
):
    settings = site_settings(db)
    if provider not in {"qq", "discord"}:
        raise HTTPException(status_code=404, detail="登录方式不存在")
    enabled = settings.get(f"auth_{provider}_enabled") == "1"
    client_id = settings.get(f"{provider}_client_id", "")
    client_secret = settings.get(f"{provider}_client_secret", "")
    if not enabled or not client_id or not client_secret:
        raise HTTPException(status_code=404, detail="这个登录方式尚未开启")
    ip = _ip(request)
    if not auth_limiter.allow(f"oauth-start:{ip}", 20, 300):
        return redirect("/login?error=" + quote("登录请求过于频繁，请稍后再试。"))
    redirect_uri = _callback_url(request, db, provider)
    state = _make_state(provider, safe_next(next, "/account"), config)
    if provider == "qq":
        target = "https://graph.qq.com/oauth2.0/authorize?" + urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "state": state,
                "scope": "get_user_info",
            }
        )
    else:
        target = "https://discord.com/oauth2/authorize?" + urlencode(
            {
                "client_id": client_id,
                "response_type": "code",
                "redirect_uri": redirect_uri,
                "scope": "identify",
                "state": state,
            }
        )
    response = RedirectResponse(target, status_code=302)
    response.set_cookie(
        "cb_oauth_state",
        state,
        max_age=600,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )
    return response


@router.get("/auth/{provider}/callback")
def oauth_callback(
    provider: str,
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    storage: Storage = Depends(get_storage),
):
    if provider not in {"qq", "discord"}:
        raise HTTPException(status_code=404, detail="登录方式不存在")
    if not _provider_state(db).get(f"{provider}_enabled"):
        return redirect("/login?error=" + quote("这个登录方式尚未开启。"))
    if error:
        return redirect("/login?error=" + quote("第三方登录被取消或失败。"))
    expected_state = request.cookies.get("cb_oauth_state", "")
    if not expected_state or not hmac.compare_digest(expected_state, state):
        return redirect("/login?error=" + quote("登录状态已失效，请重新发起。"))
    next_url = _read_state(state, provider, config)
    if not next_url or not code:
        return redirect("/login?error=" + quote("登录状态已失效，请重新发起。"))
    if not auth_limiter.allow(f"oauth-cb:{_ip(request)}", 20, 300):
        return redirect("/login?error=" + quote("登录请求过于频繁，请稍后再试。"))
    try:
        subject, nickname = _oauth_profile(
            provider, code, _callback_url(request, db, provider), db
        )
        return _oauth_login(
            db, request, config, provider, subject, nickname, next_url, storage
        )
    except Exception:
        return redirect("/login?error=" + quote("第三方登录失败，请稍后再试。"))


@router.get("/login")
def login_page(
    request: Request,
    next: str = "",
    db: sqlite3.Connection = Depends(get_db_conn),
):
    if current_user(db, request):
        return redirect(safe_next(next, "/account"))
    state = _provider_state(db)
    if not state["any_login"]:
        raise HTTPException(status_code=404, detail="登录功能尚未开启")
    return render(
        request,
        db,
        "auth/login.html",
        {
            "next": safe_next(next, "/account"),
            "providers": state,
            "page_title": "登录",
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form(""),
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
):
    require_csrf(request, csrf, config)
    state = _provider_state(db)
    if not state["password_enabled"]:
        raise HTTPException(status_code=404, detail="账号密码登录尚未开启")
    username = username.strip()
    ip = _ip(request)
    retry = login_retry_after(db, ip)
    if retry:
        return redirect(
            "/login?error=" + quote(f"尝试次数过多，请在 {cooldown_text(retry)} 后再试。")
        )
    user = db.execute(
        "SELECT * FROM users WHERE username = ? COLLATE NOCASE AND is_active = 1",
        (username,),
    ).fetchone()
    if not user or not verify_password(password, user["password_hash"]):
        delay = register_login_failure(db, ip)
        if delay:
            return redirect(
                "/login?error=" + quote(f"尝试次数过多，请在 {cooldown_text(delay)} 后再试。")
            )
        return redirect("/login?error=" + quote("用户名或密码不正确。"))
    clear_login_throttle(db, ip)
    db.execute(
        "UPDATE users SET last_login_at = ?, updated_at = ? WHERE id = ?",
        (utc_now(), utc_now(), user["id"]),
    )
    token, _ = create_session(db, int(user["id"]), request, config)
    audit(db, "user.login", actor=user["username"], ip=ip)
    db.commit()
    response = RedirectResponse(safe_next(next, "/account"), status_code=303)
    set_session_cookie(response, request, config, token)
    return response


@router.get("/register")
def register_page(
    request: Request,
    db: sqlite3.Connection = Depends(get_db_conn),
):
    state = _provider_state(db)
    if not state["register_enabled"] or not state["password_enabled"]:
        raise HTTPException(status_code=404, detail="注册功能尚未开启")
    if current_user(db, request):
        return redirect("/account")
    return render(
        request,
        db,
        "auth/register.html",
        {"providers": state, "page_title": "注册"},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/register")
def register_submit(
    request: Request,
    username: str = Form(""),
    display_name: str = Form(""),
    password: str = Form(""),
    password2: str = Form(""),
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    storage: Storage = Depends(get_storage),
):
    require_csrf(request, csrf, config)
    state = _provider_state(db)
    if not state["register_enabled"] or not state["password_enabled"]:
        raise HTTPException(status_code=404, detail="注册功能尚未开启")
    username = username.strip()
    display_name = display_name.strip()[:40]
    ip = _ip(request)
    if not auth_limiter.allow(f"register:{ip}", 10, 3600):
        return redirect("/register?error=" + quote("注册请求过于频繁，请稍后再试。"))
    if not USERNAME_RE.match(username):
        return redirect(
            "/register?error="
            + quote("用户名需为 3-64 位字母、数字或 _ . @ - 字符。")
        )
    if len(password) < 8:
        return redirect("/register?error=" + quote("密码至少需要 8 个字符。"))
    if len(password) > 200:
        return redirect("/register?error=" + quote("密码过长。"))
    if password != password2:
        return redirect("/register?error=" + quote("两次输入的密码不一致。"))
    if db.execute(
        "SELECT 1 FROM users WHERE username = ? COLLATE NOCASE", (username,)
    ).fetchone():
        return redirect("/register?error=" + quote("这个用户名已被占用。"))
    now = utc_now()
    try:
        cursor = db.execute(
            "INSERT INTO users(username, password_hash, role, display_name, "
            "is_active, quota_mb, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                username,
                hash_password(password),
                "user",
                display_name,
                1,
                default_quota_mb(db, storage),
                now,
                now,
            ),
        )
    except sqlite3.IntegrityError:
        return redirect("/register?error=" + quote("这个用户名已被占用。"))
    user_id = int(cursor.lastrowid)
    db.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now, user_id))
    token, _ = create_session(db, user_id, request, config)
    audit(db, "user.register", actor=username, target=str(user_id), ip=ip)
    db.commit()
    response = RedirectResponse("/account", status_code=303)
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
    response = RedirectResponse("/", status_code=303)
    clear_session_cookie(response)
    return response


@router.get("/account")
def account_page(
    request: Request,
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
):
    user = current_user(db, request)
    if not user:
        return redirect("/login?next=/account")
    rows = db.execute(
        asset_query()
        + "WHERE a.owner_id = ? AND a.deleted_at IS NULL "
        "ORDER BY a.id DESC",
        (user["id"],),
    ).fetchall()
    from .routes_public import _decorate_assets

    assets = _decorate_assets(
        request, db, config, request.app.state.geo, rows, False
    )
    rejected_assets = [item for item in assets if item["status"] == "rejected"]
    visible_assets = [item for item in assets if item["status"] != "rejected"]
    retention_days = rejected_retention_days(db)
    for item in rejected_assets:
        item["expiry"] = expiry_for(
            item.get("reviewed_at") or item.get("updated_at") or "",
            retention_days,
        )
    tab = "rejected" if request.query_params.get("view") == "rejected" else "works"
    return render(
        request,
        db,
        "account.html",
        {
            "account_assets": visible_assets,
            "rejected_assets": rejected_assets,
            "account_tab": tab,
            "active_count": len(visible_assets),
            "rejected_count": len(rejected_assets),
            "rejected_retention_days": retention_days,
            "account_author": user_display_name(user),
            "page_title": "我的账户",
            "all_albums": album_choices(db),
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/account")
def account_save(
    request: Request,
    display_name: str = Form(""),
    current_password: str = Form(""),
    new_password: str = Form(""),
    new_password2: str = Form(""),
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
):
    require_csrf(request, csrf, config)
    user = current_user(db, request)
    if not user:
        return redirect("/login?next=/account")
    display_name = display_name.strip()[:40]
    if new_password:
        if not verify_password(current_password, user["password_hash"]):
            return redirect("/account?error=" + quote("当前密码不正确。"))
        if len(new_password) < 8:
            return redirect("/account?error=" + quote("新密码至少需要 8 个字符。"))
        if new_password != new_password2:
            return redirect("/account?error=" + quote("两次输入的新密码不一致。"))
    now = utc_now()
    db.execute(
        "UPDATE users SET display_name = ?, password_hash = ?, updated_at = ? WHERE id = ?",
        (
            display_name,
            hash_password(new_password) if new_password else user["password_hash"],
            now,
            user["id"],
        ),
    )
    # Keep the stored fallback signature in sync for cases where the account
    # row is later unavailable.  The public author join still uses the live
    # display name whenever possible.
    author = user_display_name(
        {"username": user["username"], "role": user["role"], "display_name": display_name}
    )
    db.execute(
        "UPDATE assets SET author_name = ? WHERE owner_id = ?", (author, user["id"])
    )
    audit(db, "user.account_update", actor=user["username"], target=str(user["id"]))
    db.commit()
    return flash_redirect("/account", "账户资料已更新。")


def _owned_asset(
    db: sqlite3.Connection, user: sqlite3.Row, asset_id: int
) -> sqlite3.Row | None:
    asset = db.execute(asset_query() + "WHERE a.id = ?", (asset_id,)).fetchone()
    if not asset or asset["deleted_at"]:
        return None
    if int(asset["owner_id"] or 0) != int(user["id"]):
        return None
    return asset


@router.get("/account/assets/{asset_id}/edit")
def account_asset_edit_page(
    asset_id: int,
    request: Request,
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
):
    user = current_user(db, request)
    edit_path = f"/account/assets/{asset_id}/edit"
    if not user:
        return redirect("/login?next=" + quote(edit_path))
    asset = db.execute(asset_query() + "WHERE a.id = ?", (asset_id,)).fetchone()
    if not asset or asset["deleted_at"]:
        raise HTTPException(status_code=404, detail="图片不存在")
    # Administrators use the unrestricted admin editor, even for assets they
    # own.  The personal editor intentionally exposes a smaller field set.
    if user["role"] == "admin":
        return redirect(admin_url(request, f"/assets/{asset_id}"))
    if int(asset["owner_id"] or 0) != int(user["id"]):
        raise HTTPException(status_code=404, detail="图片不存在")
    try:
        metadata = json.loads(asset["metadata_json"] or "{}")
    except ValueError:
        metadata = {}
    return render(
        request,
        db,
        "account_asset_edit.html",
        {
            "asset": asset,
            "metadata": metadata,
            "albums": album_choices(db),
            "page_title": "图片设置",
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/account/assets/{asset_id}/edit")
def account_asset_edit_submit(
    asset_id: int,
    request: Request,
    title: str = Form(""),
    description: str = Form(""),
    intro_font_size: str = Form("15"),
    intro_font_style: str = Form("italic"),
    intro_quote: str = Form(""),
    intro_color: str = Form(""),
    album_id: str = Form(""),
    visibility: str = Form("public"),
    nsfw: str = Form(""),
    region_mode: str = Form("inherit"),
    region_countries: str = Form(""),
    password: str = Form(""),
    clear_password: str = Form(""),
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    geo: GeoResolver = Depends(get_geo),
):
    require_csrf(request, csrf, config)
    user = current_user(db, request)
    edit_path = f"/account/assets/{asset_id}/edit"
    if not user:
        return redirect("/login?next=" + quote(edit_path))
    if user["role"] == "admin":
        if db.execute(
            "SELECT 1 FROM assets WHERE id = ? AND deleted_at IS NULL", (asset_id,)
        ).fetchone():
            return redirect(admin_url(request, f"/assets/{asset_id}"))
        raise HTTPException(status_code=404, detail="图片不存在")
    asset = _owned_asset(db, user, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="图片不存在")

    new_album_id = int(album_id) if album_id.strip().isdigit() else None
    new_album = None
    if new_album_id is not None:
        new_album = db.execute(
            "SELECT * FROM albums WHERE id = ?", (new_album_id,)
        ).fetchone()
        if not new_album:
            return flash_redirect(edit_path, "目标相册不存在。")
        target_access = check_album(
            request,
            db,
            config,
            geo,
            new_album_id,
            admin=user["role"] == "admin",
        )
        if target_access.locked:
            return flash_redirect(
                edit_path, "目标相册需要密码，请先打开该相册并解锁后再移动。"
            )
        if not target_access.allowed:
            return flash_redirect(edit_path, "目标相册当前受限，无法移入。")

    if visibility not in {"public", "private"}:
        visibility = "public"
    intro_size = parse_int(intro_font_size, 15, 10, 72)
    intro_style = intro_font_style if intro_font_style in INTRO_STYLE_MAP else "italic"
    intro_color_value = intro_color.strip()[:40]
    color_invalid = bool(intro_color_value) and not INTRO_COLOR_RE.fullmatch(
        intro_color_value
    )
    if color_invalid:
        intro_color_value = ""
    password_hash = asset["access_password_hash"]
    password_note = None
    if clear_password == "1":
        password_hash = None
    elif password:
        if len(password) > 200:
            return flash_redirect(edit_path, "访问密码过长。")
        password_note = password_warning(password)
        password_hash = hash_password(password)
    experimental = get_setting(db, "experimental_features", "0") == "1"
    if not experimental:
        # Do not silently wipe an administrator-configured region when the
        # experimental UI is hidden; only the exposed fields are user-editable.
        region_mode = asset["region_mode"]
        region_countries = asset["region_countries"]
    db.execute(
        "UPDATE assets SET title = ?, description = ?, intro_font_size = ?, "
        "intro_font_style = ?, intro_quote = ?, intro_color = ?, "
        "intro_allow_html = 0, album_id = ?, box_id = ?, visibility = ?, "
        "nsfw = ?, access_password_hash = ?, region_mode = ?, "
        "region_countries = ?, updated_at = ? WHERE id = ?",
        (
            title.strip()[:300] or (asset["original_name"] or "")[:300],
            description.strip()[:5000],
            str(intro_size),
            intro_style,
            1 if intro_quote == "1" else 0,
            intro_color_value,
            new_album_id,
            int(new_album["box_id"]) if new_album and new_album["box_id"] else None,
            visibility,
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
        "user.asset_update",
        actor=user["username"],
        target=str(asset_id),
        detail=f"album={new_album_id} visibility={visibility} nsfw={nsfw}",
    )
    db.commit()
    message = "图片设置已保存。"
    if password_note:
        message += " " + password_note
    if color_invalid:
        message += " 简介颜色格式无效，已改为跟随主题颜色。"
    return flash_redirect(edit_path, message)


@router.post("/account/assets/{asset_id}")
def account_asset_save(
    asset_id: int,
    request: Request,
    album_id: str = Form(""),
    visibility: str = Form("public"),
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    geo: GeoResolver = Depends(get_geo),
):
    require_csrf(request, csrf, config)
    user = current_user(db, request)
    if not user:
        return redirect("/login?next=/account")
    asset = db.execute(
        "SELECT * FROM assets WHERE id = ? AND deleted_at IS NULL", (asset_id,)
    ).fetchone()
    if not asset or int(asset["owner_id"] or 0) != int(user["id"]):
        raise HTTPException(status_code=404, detail="图片不存在")
    if visibility not in {"public", "private"}:
        visibility = "public"
    album = None
    if album_id.strip().isdigit():
        album = db.execute(
            "SELECT * FROM albums WHERE id = ?", (int(album_id),)
        ).fetchone()
    if not album:
        return redirect("/account?error=" + quote("请选择目标相册。"))
    target_access = check_album(
        request, db, config, geo, int(album["id"]), admin=False
    )
    if target_access.locked:
        return redirect(
            "/account?error="
            + quote("目标相册需要密码，请先打开该相册并解锁后再移动。")
        )
    if not target_access.allowed:
        return redirect("/account?error=" + quote("目标相册当前受限，无法移入。"))
    db.execute(
        "UPDATE assets SET album_id = ?, box_id = ?, visibility = ?, updated_at = ? WHERE id = ?",
        (album["id"], album["box_id"], visibility, utc_now(), asset_id),
    )
    audit(
        db,
        "user.asset_move",
        actor=user["username"],
        target=str(asset_id),
        detail=f"album={album['id']} visibility={visibility}",
    )
    db.commit()
    return flash_redirect("/account", "图片已移动。")


@router.post("/account/assets/{asset_id}/delete")
def account_asset_delete(
    asset_id: int,
    request: Request,
    csrf: str = Form(""),
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
    storage: Storage = Depends(get_storage),
):
    require_csrf(request, csrf, config)
    user = current_user(db, request)
    if not user:
        return redirect("/login?next=/account")
    asset = db.execute(
        "SELECT * FROM assets WHERE id = ? AND deleted_at IS NULL", (asset_id,)
    ).fetchone()
    if not asset or int(asset["owner_id"] or 0) != int(user["id"]):
        raise HTTPException(status_code=404, detail="图片不存在")
    purged = delete_asset(db, storage, asset_id, purge=False)
    audit(
        db,
        "user.asset_delete",
        actor=user["username"],
        target=str(asset_id),
        detail=f"purged={purged}",
    )
    db.commit()
    return flash_redirect(
        "/account", "图片已永久删除。" if purged else "图片已移入回收站。"
    )


@router.get("/authors/{user_id}")
def author_page(
    user_id: int,
    request: Request,
    db: sqlite3.Connection = Depends(get_db_conn),
    config: Config = Depends(get_config),
):
    author = db.execute(
        "SELECT id, username, display_name, role, is_active FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if not author or not int(author["is_active"]):
        raise HTTPException(status_code=404, detail="作者不存在")
    viewer = current_user(db, request)
    is_owner = bool(viewer and int(viewer["id"]) == int(author["id"]))
    if is_owner:
        where = "a.owner_id = ? AND a.deleted_at IS NULL AND a.status != 'rejected'"
    else:
        where = (
            "a.owner_id = ? AND a.deleted_at IS NULL "
            "AND a.status = 'approved' AND a.visibility = 'public'"
        )
    rows = db.execute(
        asset_query() + f"WHERE {where} ORDER BY a.id DESC",
        (author["id"],),
    ).fetchall()
    from .routes_public import _decorate_assets

    assets = _decorate_assets(
        request, db, config, request.app.state.geo, rows, False
    )
    return render(
        request,
        db,
        "author.html",
        {
            "author": author,
            "author_name": user_display_name(author),
            "author_assets": assets,
            "is_owner": is_owner,
            "page_title": user_display_name(author),
        },
    )