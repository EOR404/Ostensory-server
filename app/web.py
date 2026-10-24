from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from .config import Config
from .database import all_settings
from .updates import UPDATE_DEFAULTS, update_state
from .i18n import (
    LANGUAGE_COOKIE_MAX_AGE,
    js_strings,
    request_language,
    translate,
)
from .security import (
    cookie_secure,
    csrf_from_request,
    current_admin,
    current_user,
    signed_download_url,
    signed_view_url,
    verify_csrf_token,
)

TEMPLATE_GLOBALS: dict[str, Any] = {}

DEFAULT_SETTINGS: dict[str, str] = {
    "site_name": "Ostensory",
    "site_subtitle": "ComfyUI 作品与生成参数归档",
    "default_language": "auto",
    "default_theme": "dark",
    "site_description": "私人 ComfyUI 图片与生成参数归档",
    "site_icon_url": "",
    "accent_color": "#d7a76c",
    "public_base_url": "",
    "experimental_features": "0",
    "external_image_enabled": "0",
    "duplicate_check_completed": "0",
    "show_duplicate_images": "1",
    "duplicate_checked_at": "",
    "cursor_style": "default",
    "cursor_custom_url": "",
    "cursor_hotspot_x": "4",
    "cursor_hotspot_y": "4",
    "blur_nsfw": "1",
    "nsfw_blocked_countries": '["CN"]',
    "geoip_fail_mode": "block",
    "per_page": "48",
    "max_upload_mb": "200",
    "allowed_extensions": "png,jpg,jpeg,webp,gif",
    "client_download_enabled": "1",
    "default_album_path": "未分类",
    "admin_path": "admin",
    "browse_mode": "pagination",
    "albums_layout": "rows",
    "browse_layout": "masonry",
    "stats_enabled": "1",
    "stats_show_counts": "1",
    "stats_show_storage": "1",
    "stats_show_metadata": "1",
    "stats_show_activity": "1",
    "stats_show_albums": "1",
    "stats_show_guest": "0",
    "stats_show_engagement": "1",
    "stats_show_like_rank": "1",
    "stats_show_view_rank": "1",
    "likes_views_enabled": "1",
    "allow_anonymous_likes": "0",
    "registration_enabled": "0",
    "auth_password_enabled": "1",
    "auth_qq_enabled": "0",
    "auth_discord_enabled": "0",
    "qq_client_id": "",
    "qq_client_secret": "",
    "discord_client_id": "",
    "discord_client_secret": "",
    "user_upload_enabled": "0",
    "upload_require_review": "0",
    "rejected_retention_days": "7",
    "preview_enabled": "1",
    "user_quota_mode": "fixed",
    "user_quota_default_mb": "1024",
    "user_quota_full_action": "reject",
    "trash_retention_days": "-1",
    "external_image_guest_enabled": "1",
    "guest_upload_enabled": "0",
    "guest_upload_log_ip": "0",
    "guest_upload_key_mode": "off",
    "guest_upload_key_hash": "",
    "guest_upload_once_code": "",
    "guest_upload_once_hash": "",
    "guest_upload_once_used": "0",
    "guest_upload_album_ids": "",
    "guest_upload_max_mb": "20",
    "guest_upload_quota_mb": "1024",
    "guest_upload_cycle_enabled": "0",
    "guest_upload_cycle_scope": "album",
    "guest_upload_interval_seconds": "30",
    "guest_upload_max_files": "20",
    "geoip_public_update_enabled": "0",
    "home_mode": "waterfall",
    "home_layout": "masonry",
    "home_carousel_kind": "assets",
    "home_carousel_ids": "",
    "home_carousel_interval": "6",
    "home_waterfall_source": "latest",
    "home_selected_ids": "",
    "footer_icp": "",
    "footer_icp_url": "https://beian.miit.gov.cn/",
    "footer_html": "",
    "custom_css": "",
    "custom_head_html": "",
    "about_enabled": "0",
    "about_name": "",
    "about_avatar": "",
    "about_bio": "",
    "about_html": "",
    **UPDATE_DEFAULTS,
}


def admin_prefix(request: Request) -> str:
    return getattr(request.app.state, "admin_path", "admin")


def admin_url(request: Request, suffix: str = "") -> str:
    prefix = admin_prefix(request)
    if not suffix or suffix == "/":
        return f"/{prefix}"
    return f"/{prefix}/{suffix.lstrip('/')}"


def public_base(request: Request, db: sqlite3.Connection) -> str:
    configured = ""
    row = db.execute("SELECT value FROM settings WHERE key = 'public_base_url'").fetchone()
    if row:
        configured = row["value"].strip()
    return (configured or str(request.base_url)).rstrip("/")


def raw_settings(db: sqlite3.Connection) -> dict[str, str]:
    return {**DEFAULT_SETTINGS, **all_settings(db)}


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def site_settings(db: sqlite3.Connection) -> dict[str, Any]:
    raw: dict[str, Any] = raw_settings(db)
    ints = {"per_page": 48, "max_upload_mb": 200, "home_carousel_interval": 6}
    for key, default in ints.items():
        try:
            raw[key] = int(raw.get(key, default))
        except (TypeError, ValueError):
            raw[key] = default
    raw["blur_nsfw"] = _truthy(raw.get("blur_nsfw", "1"))
    raw["client_download_enabled"] = _truthy(raw.get("client_download_enabled", "1"))
    raw["about_enabled"] = _truthy(raw.get("about_enabled", "0"))
    raw["per_page"] = max(6, min(200, int(raw["per_page"])))
    raw["max_upload_mb"] = max(1, min(2048, int(raw["max_upload_mb"])))
    raw["home_carousel_interval"] = max(2, min(30, int(raw["home_carousel_interval"])))
    if raw.get("home_mode") not in {"waterfall", "carousel", "carousel_waterfall"}:
        raw["home_mode"] = "waterfall"
    if raw.get("home_carousel_kind") not in {"assets", "albums"}:
        raw["home_carousel_kind"] = "assets"
    if raw.get("home_waterfall_source") not in {"latest", "selected", "all"}:
        raw["home_waterfall_source"] = "latest"
    if raw.get("browse_mode") not in {"pagination", "infinite"}:
        raw["browse_mode"] = "pagination"
    if raw.get("home_layout") not in {"masonry", "grid"}:
        raw["home_layout"] = "masonry"
    if raw.get("cursor_style") not in {
        "default",
        "ink",
        "ring",
        "cross",
        "diamond",
        "custom",
    }:
        raw["cursor_style"] = "default"
    return raw


def safe_next(value: str | None, default: str = "/") -> str:
    if not value:
        return default
    if (
        value.startswith("/")
        and not value.startswith("//")
        and "\\" not in value
        and "\r" not in value
        and "\n" not in value
    ):
        return value
    return default


def render(
    request: Request,
    db: sqlite3.Connection,
    template: str,
    context: dict[str, Any] | None = None,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> Response:
    config: Config = request.app.state.config
    settings = site_settings(db)
    language = request_language(request)
    admin = context.get("admin") if context and "admin" in context else current_admin(db, request)
    user = current_user(db, request)
    csrf = csrf_from_request(request, config)
    ctx = dict(context or {})
    ctx.update(
        {
            "request": request,
            "settings": settings,
            "site": settings,
            "admin": admin,
            "user": user,
            "is_admin": bool(admin),
            "is_user": bool(user),
            "admin_prefix": admin_prefix(request),
            "admin_url": lambda suffix="": admin_url(request, suffix),
            "base_url": public_base(request, db),
            "cursor_css_value": cursor_css_value(settings),
            "csrf_token": csrf,
            "flash": request.query_params.get("msg", ""),
            "error": request.query_params.get("error", ""),
            "current_path": request.url.path,
            "year": datetime.now(timezone.utc).year,
            "noindex": False,
            "lang": language,
            "language_cookie_max_age": LANGUAGE_COOKIE_MAX_AGE,
            "language_code": "zh-CN" if language == "zh" else "en",
            "t": lambda key, **values: translate(language, key, **values),
            "js_i18n": js_strings(language),
            "view_url": lambda asset_id: signed_view_url(asset_id, config),
            "download_url": lambda asset_id: signed_download_url(asset_id, config),
            "update_state": update_state(db, settings),
        }
    )
    templates = request.app.state.templates
    response = templates.TemplateResponse(request, template, ctx, status_code=status_code, headers=headers)
    response.headers.setdefault("Vary", "Accept-Language, Cookie")
    existing = request.cookies.get("cb_csrf")
    if not existing or existing != csrf or not verify_csrf_token(existing, config):
        response.set_cookie(
            "cb_csrf",
            csrf,
            max_age=86400,
            httponly=True,
            samesite="lax",
            secure=cookie_secure(request, config),
            path="/",
        )
    return response


def redirect(url: str, status_code: int = 303) -> RedirectResponse:
    return RedirectResponse(url, status_code=status_code)


def flash_redirect(url: str, message: str) -> RedirectResponse:
    from urllib.parse import quote

    sep = "&" if "?" in url else "?"
    return redirect(f"{url}{sep}msg={quote(message)}")


def parse_int(value: str | None, default: int, minimum: int = 0, maximum: int = 10**9) -> int:
    try:
        number = int(value or default)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


CURSOR_URL_RE = re.compile(r"^(?:https?://[^\s\"'()\\<>]+|/(?!/)[^\s\"'()\\<>]*)$")
INTRO_COLOR_RE = re.compile(
    r"^(?:#[0-9a-fA-F]{3,8}|(?:rgb|hsl)a?\([0-9a-zA-Z.,%\s/]+\)|[a-zA-Z]{3,24})$"
)
INTRO_STYLE_MAP = {
    "italic": ("italic", "400"),
    "normal": ("normal", "400"),
    "bold": ("normal", "700"),
    "italic_bold": ("italic", "700"),
}


def intro_style(asset: Any) -> str:
    """Build a small, validated inline style for an asset introduction."""
    raw = asset.get("intro_font_size") if isinstance(asset, dict) else asset["intro_font_size"]
    try:
        size = int(raw)
    except (TypeError, ValueError):
        size = 15
    size = max(10, min(72, size))
    raw_style = asset.get("intro_font_style") if isinstance(asset, dict) else asset["intro_font_style"]
    font_style, font_weight = INTRO_STYLE_MAP.get(str(raw_style or ""), INTRO_STYLE_MAP["italic"])
    raw_color = asset.get("intro_color") if isinstance(asset, dict) else asset["intro_color"]
    color = str(raw_color or "").strip()
    if not INTRO_COLOR_RE.fullmatch(color):
        color = ""
    parts = [
        f"font-size:{size}px",
        f"font-style:{font_style}",
        f"font-weight:{font_weight}",
    ]
    if color:
        parts.append(f"color:{color}")
    return ";".join(parts)


def cursor_css_value(settings: dict[str, Any]) -> str:
    """Return a validated CSS cursor value for the custom preset only."""
    if str(settings.get("cursor_style")) != "custom":
        return ""
    url = str(settings.get("cursor_custom_url") or "").strip()
    if not CURSOR_URL_RE.fullmatch(url):
        return ""
    x = parse_int(str(settings.get("cursor_hotspot_x")), 4, 0, 64)
    y = parse_int(str(settings.get("cursor_hotspot_y")), 4, 0, 64)
    return f"url('{url}') {x} {y}, auto"


def format_datetime(value: str | None) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return value


def format_size(value: int | None) -> str:
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def json_pretty(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def media_url(asset_id: int, kind: str = "preview", version: str = "") -> str:
    suffix = f"?v={version[:12]}" if version else ""
    return f"/media/{kind}/{asset_id}{suffix}"


def asset_url(asset_id: int, source: str = "all") -> str:
    suffix = "?from=album" if source == "album" else ""
    return f"/images/{asset_id}{suffix}"


def download_url(asset_id: int) -> str:
    return f"/download/{asset_id}"


def external_image_url(asset_id: int) -> str:
    return f"/embed/{asset_id}"


def relative_path(path: str, base: Path) -> str:
    try:
        return Path(path).relative_to(base).as_posix()
    except ValueError:
        return path