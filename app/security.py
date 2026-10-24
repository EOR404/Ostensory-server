from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

from starlette.requests import Request
from starlette.responses import Response

from .config import Config
from .database import utc_now

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32

SESSION_COOKIE = "cb_session"
CSRF_COOKIE = "cb_csrf"
UNLOCK_COOKIE = "cb_unlock"
VIEW_TOKEN_TTL = 15 * 60

# Login protection is deliberately per-client-IP, not per-account.  A wrong
# password from one address must never lock the real administrator out when
# they sign in from another address.
LOGIN_FREE_ATTEMPTS = 5
LOGIN_BASE_DELAY_SECONDS = 10
LOGIN_MAX_DELAY_SECONDS = 30 * 60
LOGIN_FAILURE_WINDOW_SECONDS = 24 * 60 * 60


def view_token(asset_id: int, config: Config, ttl: int = VIEW_TOKEN_TTL) -> str:
    """Create a short-lived token for the internal original-image viewer.

    The token only proves that this server issued a view link recently.  It is
    intentionally short-lived so a copied URL cannot keep working forever when
    external hotlinking is turned off.
    """
    expires = int(time.time()) + max(60, int(ttl))
    message = f"view:{int(asset_id)}:{expires}".encode("ascii")
    digest = hmac.new(config.secret_key, message, hashlib.sha256).hexdigest()
    return f"{expires}.{digest}"


def verify_view_token(asset_id: int, token: str, config: Config) -> bool:
    try:
        expires_text, digest = (token or "").split(".", 1)
        expires = int(expires_text)
    except (ValueError, AttributeError):
        return False
    if expires < int(time.time()):
        return False
    message = f"view:{int(asset_id)}:{expires}".encode("ascii")
    expected = hmac.new(config.secret_key, message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, expected)


def signed_view_url(asset_id: int, config: Config) -> str:
    return f"/view/{int(asset_id)}?token={view_token(asset_id, config)}"


def download_token(asset_id: int, config: Config, ttl: int = VIEW_TOKEN_TTL) -> str:
    """Short-lived token for a browser download link.

    The download itself is an ordinary file response, but a copied URL should
    not stay useful forever after the owner has disabled external images.
    """
    expires = int(time.time()) + max(60, int(ttl))
    message = f"download:{int(asset_id)}:{expires}".encode("ascii")
    digest = hmac.new(config.secret_key, message, hashlib.sha256).hexdigest()
    return f"{expires}.{digest}"


def verify_download_token(asset_id: int, token: str, config: Config) -> bool:
    try:
        expires_text, digest = (token or "").split(".", 1)
        expires = int(expires_text)
    except (ValueError, AttributeError):
        return False
    if expires < int(time.time()):
        return False
    message = f"download:{int(asset_id)}:{expires}".encode("ascii")
    expected = hmac.new(config.secret_key, message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, expected)


def signed_download_url(asset_id: int, config: Config) -> str:
    return f"/download/{int(asset_id)}?token={download_token(asset_id, config)}"


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def hash_password(password: str) -> str:
    if not isinstance(password, str) or not password:
        raise ValueError("密码不能为空")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64e(salt)}${_b64e(digest)}"


def verify_password(password: str, stored: str | None) -> bool:
    if not password or not stored:
        return False
    try:
        scheme, n_s, r_s, p_s, salt_s, digest_s = stored.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = _b64d(salt_s)
        expected = _b64d(digest_s)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
            maxmem=64 * 1024 * 1024,
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, OverflowError):
        return False


def password_warning(password: str) -> str | None:
    """Return a non-blocking reminder for short passwords.

    Password length is a deliberate user choice here; only an empty value is
    invalid because there would be nothing to verify.
    """
    if not password:
        return "密码不能为空。"
    if len(password) < 10:
        return "密码较短，建议至少 10 个字符；已按你输入的内容保存。"
    return None


def sha256_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_random_token(prefix: str = "") -> str:
    return prefix + secrets.token_urlsafe(32)


def create_session(
    db: sqlite3.Connection,
    user_id: int,
    request: Request,
    config: Config,
) -> tuple[str, datetime]:
    token = new_random_token()
    expires = datetime.now(timezone.utc) + timedelta(days=max(1, config.session_days))
    db.execute(
        "INSERT INTO sessions(token_hash, user_id, created_at, expires_at, ip, user_agent) "
        "VALUES(?,?,?,?,?,?)",
        (
            sha256_token(token),
            user_id,
            utc_now(),
            expires.isoformat(timespec="seconds").replace("+00:00", "Z"),
            getattr(request.state, "client_ip", "") or (request.client.host if request.client else ""),
            request.headers.get("user-agent", "")[:300],
        ),
    )
    db.commit()
    return token, expires


def get_session_user(db: sqlite3.Connection, token: str | None) -> sqlite3.Row | None:
    if not token:
        return None
    row = db.execute(
        "SELECT u.*, s.token_hash, s.expires_at FROM sessions s "
        "JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
        (sha256_token(token),),
    ).fetchone()
    if not row:
        return None
    try:
        expires = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
    except ValueError:
        return None
    if expires < datetime.now(timezone.utc):
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (sha256_token(token),))
        return None
    return row


def current_session_token(request: Request) -> str | None:
    return request.cookies.get(SESSION_COOKIE)


def current_admin(db: sqlite3.Connection, request: Request) -> sqlite3.Row | None:
    return get_session_user(db, current_session_token(request))


def delete_session(db: sqlite3.Connection, token: str | None) -> None:
    if token:
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (sha256_token(token),))


def create_api_token(db: sqlite3.Connection, user_id: int, name: str) -> str:
    token = new_random_token("cbk_")
    db.execute(
        "INSERT INTO api_tokens(user_id, name, token_hash, created_at) VALUES(?,?,?,?)",
        (user_id, (name or "客户端").strip()[:80], sha256_token(token), utc_now()),
    )
    return token


def authenticate_api_token(db: sqlite3.Connection, token: str | None) -> sqlite3.Row | None:
    if not token or not token.startswith("cbk_"):
        return None
    row = db.execute(
        "SELECT t.*, u.username FROM api_tokens t JOIN users u ON u.id = t.user_id "
        "WHERE t.token_hash = ? AND t.revoked_at IS NULL",
        (sha256_token(token),),
    ).fetchone()
    if not row:
        return None
    db.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (utc_now(), row["id"]))
    return row


def parse_basic_auth(header: str | None) -> tuple[str, str] | None:
    if not header or not header.lower().startswith("basic "):
        return None
    try:
        raw = base64.b64decode(header[6:].strip()).decode("utf-8")
        username, password = raw.split(":", 1)
        return username, password
    except (ValueError, UnicodeDecodeError):
        return None


# ---------------------------------------------------------------------------
# CSRF double-submit cookie
# ---------------------------------------------------------------------------


def _sign(value: str, secret: bytes) -> str:
    sig = hmac.new(secret, value.encode("utf-8"), hashlib.sha256).digest()
    return _b64e(sig)


def make_csrf_token(config: Config) -> str:
    raw = secrets.token_urlsafe(24)
    return f"{raw}.{_sign(raw, config.secret_key)}"


def verify_csrf_token(value: str | None, config: Config) -> bool:
    if not value or "." not in value:
        return False
    raw, sig = value.rsplit(".", 1)
    return hmac.compare_digest(sig, _sign(raw, config.secret_key))


def csrf_from_request(request: Request, config: Config) -> str:
    value = request.cookies.get(CSRF_COOKIE)
    if value and verify_csrf_token(value, config):
        return value
    return make_csrf_token(config)


def check_csrf(request: Request, form_token: str | None, config: Config) -> bool:
    cookie = request.cookies.get(CSRF_COOKIE)
    return bool(
        cookie
        and form_token
        and verify_csrf_token(cookie, config)
        and hmac.compare_digest(cookie, form_token)
    )


# ---------------------------------------------------------------------------
# Unlock tickets for password-protected albums/assets
# ---------------------------------------------------------------------------


def _unlock_payload(request: Request, config: Config) -> dict[str, Any]:
    raw = request.cookies.get(UNLOCK_COOKIE)
    if not raw or "." not in raw:
        return {"scopes": [], "exp": 0}
    body, sig = raw.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(body, config.secret_key)):
        return {"scopes": [], "exp": 0}
    try:
        payload = json.loads(_b64d(body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"scopes": [], "exp": 0}
    if int(payload.get("exp", 0)) < int(time.time()):
        return {"scopes": [], "exp": 0}
    scopes = payload.get("scopes")
    if not isinstance(scopes, list):
        scopes = []
    return {"scopes": [str(x) for x in scopes], "exp": int(payload.get("exp", 0))}


def unlock_scopes(request: Request, config: Config) -> set[str]:
    return set(_unlock_payload(request, config)["scopes"])


def set_unlock_cookie(
    response: Response,
    request: Request,
    config: Config,
    add_scope: str | None = None,
    ttl_seconds: int = 12 * 3600,
) -> None:
    payload = _unlock_payload(request, config)
    scopes = set(payload["scopes"])
    if add_scope:
        scopes.add(add_scope)
    exp = max(int(time.time()) + ttl_seconds, payload["exp"])
    body = _b64e(json.dumps({"scopes": sorted(scopes), "exp": exp}, separators=(",", ":")).encode())
    value = f"{body}.{_sign(body, config.secret_key)}"
    response.set_cookie(
        UNLOCK_COOKIE,
        value,
        max_age=max(60, exp - int(time.time())),
        httponly=True,
        samesite="lax",
        secure=cookie_secure(request, config),
        path="/",
    )


def clear_unlock_cookie(response: Response) -> None:
    response.delete_cookie(UNLOCK_COOKIE, path="/")


# ---------------------------------------------------------------------------
# Cookies / rate limit
# ---------------------------------------------------------------------------


def cookie_secure(request: Request, config: Config) -> bool:
    if config.secure_cookies in {"1", "true", "yes", "on"}:
        return True
    if config.secure_cookies in {"0", "false", "no", "off"}:
        return False
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


def set_session_cookie(response: Response, request: Request, config: Config, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max(3600, config.session_days * 86400),
        httponly=True,
        samesite="lax",
        secure=cookie_secure(request, config),
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        now = time.time()
        with self._lock:
            bucket = self._hits.setdefault(key, deque())
            while bucket and bucket[0] < now - window_seconds:
                bucket.popleft()
            if len(bucket) >= limit:
                return False
            bucket.append(now)
            if len(self._hits) > 5000:
                self._hits.clear()
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


def _login_throttle_key(ip: str) -> str:
    # Keep the key compact and stable for IPv4 and IPv6.  Empty peer addresses
    # share a bucket instead of bypassing the guard.
    return (str(ip or "").strip() or "unknown")[:200]


def login_retry_after(db: sqlite3.Connection, ip: str, now: int | None = None) -> int:
    """Return how many seconds this client IP must wait before trying again."""
    current = int(time.time()) if now is None else int(now)
    key = _login_throttle_key(ip)
    row = db.execute(
        "SELECT failures, last_failure_at, blocked_until FROM login_throttle WHERE client_key = ?",
        (key,),
    ).fetchone()
    if not row:
        return 0
    if int(row["blocked_until"] or 0) > current:
        return max(1, int(row["blocked_until"]) - current)
    if int(row["last_failure_at"] or 0) < current - LOGIN_FAILURE_WINDOW_SECONDS:
        db.execute("DELETE FROM login_throttle WHERE client_key = ?", (key,))
        db.commit()
        return 0
    return 0


def register_login_failure(
    db: sqlite3.Connection, ip: str, now: int | None = None
) -> int:
    """Record a wrong password and return the new cooldown in seconds.

    The first few failures are free.  Starting with the fifth failure the wait
    grows exponentially (10s, 20s, 40s, ...) and is capped at 30 minutes.  A
    successful login clears the row for that IP.
    """
    current = int(time.time()) if now is None else int(now)
    key = _login_throttle_key(ip)
    # Increment in SQL rather than reading and writing two separate values:
    # parallel login attempts must not overwrite each other's failure count.
    db.execute(
        "INSERT INTO login_throttle(client_key, failures, last_failure_at, blocked_until) "
        "VALUES(?,1,?,0) ON CONFLICT(client_key) DO UPDATE SET "
        "failures = CASE WHEN login_throttle.last_failure_at >= ? "
        "THEN login_throttle.failures + 1 ELSE 1 END, "
        "last_failure_at = excluded.last_failure_at",
        (key, current, current - LOGIN_FAILURE_WINDOW_SECONDS),
    )
    row = db.execute(
        "SELECT failures FROM login_throttle WHERE client_key = ?", (key,)
    ).fetchone()
    failures = int(row[0] or 1) if row else 1
    delay = 0
    if failures >= LOGIN_FREE_ATTEMPTS:
        exponent = min(failures - LOGIN_FREE_ATTEMPTS, 10)
        delay = min(LOGIN_MAX_DELAY_SECONDS, LOGIN_BASE_DELAY_SECONDS * (2**exponent))
    blocked_until = current + delay
    db.execute(
        "UPDATE login_throttle SET blocked_until = ? WHERE client_key = ?",
        (blocked_until, key),
    )
    # Avoid retaining stale attacker rows forever.  This is cheap because the
    # table only has one row per recently seen address.
    db.execute(
        "DELETE FROM login_throttle WHERE last_failure_at < ?",
        (current - 7 * LOGIN_FAILURE_WINDOW_SECONDS,),
    )
    db.commit()
    return delay


def clear_login_throttle(db: sqlite3.Connection, ip: str | None = None) -> None:
    if ip is None:
        db.execute("DELETE FROM login_throttle")
    else:
        db.execute(
            "DELETE FROM login_throttle WHERE client_key = ?",
            (_login_throttle_key(ip),),
        )
    db.commit()


def cooldown_text(seconds: int) -> str:
    total = max(1, int(seconds))
    minutes, remainder = divmod(total, 60)
    if minutes and remainder:
        return f"{minutes} 分 {remainder} 秒"
    if minutes:
        return f"{minutes} 分钟"
    return f"{total} 秒"


unlock_limiter = RateLimiter()
guest_upload_limiter = RateLimiter()


def token_preview(token: str) -> str:
    return token[:10] + "…" + token[-4:]