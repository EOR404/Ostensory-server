from __future__ import annotations

"""Safe, direct-to-latest updates from GitHub Releases.

The updater only replaces application code.  The data directory (database,
images, keys and all settings) is never extracted into or deleted by this
module.  A consistent SQLite snapshot and a copy of the old code are created
before anything is replaced, so a failed update can be rolled back manually.
"""

import hashlib
import html
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from . import __version__
from .config import Config
from .database import get_setting, set_setting, utc_now

LOGGER = logging.getLogger(__name__)

DEFAULT_REPO = "EOR404/Ostensory-server"
DEFAULT_API = "https://api.github.com"
DEFAULT_WEB = "https://github.com"
ARCHIVE_ASSET = "ostensory-server.tar.gz"
CHECKSUM_ASSET = "SHA256SUMS.txt"
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
CHECK_INTERVAL_SECONDS = 6 * 3600
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_EXTRACTED_BYTES = 160 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 5000
REQUEST_TIMEOUT = 20
USER_AGENT = f"Ostensory-Updater/{__version__}"

UPDATE_DEFAULTS: dict[str, str] = {
    "update_auto_check": "1",
    "update_auto_install": "0",
    "update_last_check_at": "",
    "update_latest_version": "",
    "update_latest_tag": "",
    "update_latest_url": "",
    "update_latest_notes": "",
    "update_last_error": "",
    "update_installed_version": "",
    "update_last_install_at": "",
    "update_needs_restart": "0",
    "update_backup_path": "",
}

UPDATE_SETTING_KEYS = tuple(UPDATE_DEFAULTS)
CODE_ITEMS = (
    "app",
    "deploy",
    "run.py",
    "requirements.txt",
    "pyproject.toml",
    "README.md",
    "LICENSE",
)
ARCHIVE_NAME_RE = re.compile(r"^ostensory-server.*\.tar\.gz$", re.IGNORECASE)
CHECKSUM_NAME_RE = re.compile(r"(sha256|checksums?|SHA256SUMS)", re.IGNORECASE)
VERSION_RE = re.compile(r"^[vV]?(\d+(?:\.\d+)*)(?:[-_.+]?([0-9A-Za-z][0-9A-Za-z.-]*))?$")
INIT_VERSION_RE = re.compile(
    r'^__version__\s*=\s*["\']([^"\']+)["\']', re.MULTILINE
)
_HEX256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_LOCAL_UPDATE_LOCK = threading.Lock()


class UpdateError(RuntimeError):
    """An update failure that is safe to show to an administrator."""


class NoReleaseAvailable(UpdateError):
    """The repository has no eligible release yet; not a network failure."""


@dataclass(frozen=True)
class Release:
    version: str
    tag: str
    html_url: str
    notes: str
    archive_url: str
    checksum: str
    published_at: str = ""
    prerelease: bool = False


@dataclass(frozen=True)
class InstallResult:
    old_version: str
    new_version: str
    backup_path: Path
    requirements_changed: bool
    restart_required: bool


def current_version() -> str:
    return __version__


def normalize_version(value: str) -> str:
    return str(value or "").strip().lstrip("vV")


def version_key(value: str) -> tuple[tuple[int, ...], int, str] | None:
    """Return a sortable key without adding a packaging dependency.

    Release > rc > beta > alpha > dev.  Missing components are padded so
    ``1.2`` and ``1.2.0`` compare equal.
    """
    match = VERSION_RE.match(str(value or "").strip())
    if not match:
        return None
    numbers = [int(part) for part in match.group(1).split(".")]
    while len(numbers) < 12:
        numbers.append(0)
    suffix = (match.group(2) or "").lower().split("+", 1)[0]
    if not suffix:
        rank = 4
        suffix = ""
    elif suffix.startswith("dev"):
        rank = 0
    elif suffix.startswith(("a", "alpha")):
        rank = 1
    elif suffix.startswith(("b", "beta")):
        rank = 2
    elif suffix.startswith(("rc", "c", "pre")):
        rank = 3
    else:
        # Unknown suffixes are treated as prereleases but still compared.
        rank = 0
    return tuple(numbers[:12]), rank, suffix


def is_newer(candidate: str, current: str) -> bool:
    new_key = version_key(candidate)
    old_key = version_key(current)
    if new_key is None:
        return False
    if old_key is None:
        return True
    return new_key > old_key


def versions_equal(left: str, right: str) -> bool:
    left_key = version_key(left)
    right_key = version_key(right)
    return left_key is not None and left_key == right_key


def _repo() -> str:
    repo = os.environ.get("OSTENSORY_UPDATE_REPO", DEFAULT_REPO).strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise UpdateError("更新仓库名称配置不正确，应为 owner/repo。")
    return repo


def _api_root() -> str:
    raw = os.environ.get("OSTENSORY_UPDATE_API", DEFAULT_API).strip().rstrip("/")
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise UpdateError("更新 API 地址不正确。")
    if (
        parsed.scheme != "https"
        and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        and os.environ.get("OSTENSORY_UPDATE_ALLOW_HTTP") != "1"
    ):
        raise UpdateError("更新 API 必须使用 HTTPS；本机测试可显式设置 OSTENSORY_UPDATE_ALLOW_HTTP=1。")
    return raw


def _safe_web_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(str(value or "").strip())
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        return ""
    if parsed.scheme == "http" and parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        return ""
    return str(value)


def _http_fetch(url: str, *, limit: int, accept: str = "*/*") -> tuple[bytes, str]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        raise UpdateError("下载地址不是有效的 HTTP(S) 地址。")
    if (
        parsed.scheme != "https"
        and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        and os.environ.get("OSTENSORY_UPDATE_ALLOW_HTTP") != "1"
    ):
        raise UpdateError("更新下载必须使用 HTTPS。")
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": accept,
    }
    token = os.environ.get("OSTENSORY_UPDATE_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            final_url = response.geturl()
            final_parsed = urllib.parse.urlsplit(final_url)
            if (
                final_parsed.scheme != "https"
                and final_parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                and os.environ.get("OSTENSORY_UPDATE_ALLOW_HTTP") != "1"
            ):
                raise UpdateError("下载被重定向到非 HTTPS 地址，已停止。")
            payload = response.read(limit + 1)
            if len(payload) > limit:
                raise UpdateError("更新响应超过大小限制，已停止。")
            return payload, final_url
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError("仓库还没有可用的 Release。") from exc
        if exc.code in {401, 403, 429}:
            raise UpdateError(
                "GitHub 拒绝了更新请求（可能是频率限制或仓库权限）。"
                "更新检查默认使用不受 API 限额影响的 Releases 订阅；"
                "如果仍然失败，请稍后再试，或配置 OSTENSORY_UPDATE_TOKEN。"
            ) from exc
        raise UpdateError(f"更新服务器返回 HTTP {exc.code}。") from exc
    except urllib.error.URLError as exc:
        raise UpdateError(f"无法连接更新服务器：{exc.reason}") from exc
    except TimeoutError as exc:
        raise UpdateError("连接更新服务器超时。") from exc


def _http_get(url: str, *, limit: int, accept: str = "*/*") -> bytes:
    return _http_fetch(url, limit=limit, accept=accept)[0]


def _get_json(url: str) -> Any:
    payload = _http_get(
        url,
        limit=2 * 1024 * 1024,
        accept="application/vnd.github+json",
    )
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError("更新服务器返回了无法识别的数据。") from exc


def _checksum_from_text(text: str, archive_name: str) -> str:
    found: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) < 2:
            continue
        digest, name = parts[0].lstrip("*"), parts[-1].lstrip("*")
        if _HEX256_RE.fullmatch(digest):
            found[Path(name).name] = digest.lower()
    if archive_name in found:
        return found[archive_name]
    if len(found) == 1:
        return next(iter(found.values()))
    return ""


def _release_from_payload(payload: dict[str, Any]) -> Release | None:
    if not isinstance(payload, dict) or payload.get("draft"):
        return None
    tag = str(payload.get("tag_name") or "").strip()
    version = normalize_version(tag)
    if version_key(version) is None:
        return None
    assets = payload.get("assets") or []
    if not isinstance(assets, list):
        return None
    archives = [
        asset
        for asset in assets
        if isinstance(asset, dict)
        and ARCHIVE_NAME_RE.match(str(asset.get("name") or ""))
    ]
    if not archives:
        return None
    archives.sort(
        key=lambda item: (
            str(item.get("name") or "").lower() != "ostensory-server.tar.gz",
            str(item.get("name") or ""),
        )
    )
    archive = archives[0]
    archive_name = str(archive.get("name") or "")
    archive_url = str(archive.get("browser_download_url") or "")
    if not archive_url:
        return None
    size = archive.get("size")
    if isinstance(size, int) and size > MAX_ARCHIVE_BYTES:
        raise UpdateError("最新发布包超过更新大小限制。")
    checksum = ""
    digest = str(archive.get("digest") or "")
    if digest.lower().startswith("sha256:"):
        possible = digest.split(":", 1)[1].strip()
        if _HEX256_RE.fullmatch(possible):
            checksum = possible.lower()
    if not checksum:
        checksum_assets = [
            asset
            for asset in assets
            if isinstance(asset, dict)
            and CHECKSUM_NAME_RE.search(str(asset.get("name") or ""))
        ]
        for asset in checksum_assets:
            checksum_url = str(asset.get("browser_download_url") or "")
            if not checksum_url:
                continue
            text = _http_get(
                checksum_url,
                limit=256 * 1024,
                accept="text/plain",
            ).decode("utf-8", "replace")
            checksum = _checksum_from_text(text, archive_name)
            if checksum:
                break
    if not checksum:
        raise UpdateError("最新发布包没有 SHA256 校验值，出于安全考虑拒绝自动更新。")
    return Release(
        version=version,
        tag=tag,
        html_url=_safe_web_url(str(payload.get("html_url") or "")),
        notes=str(payload.get("body") or "")[:12000],
        archive_url=archive_url,
        checksum=checksum,
        published_at=str(payload.get("published_at") or ""),
        prerelease=bool(payload.get("prerelease")),
    )


def _allow_prerelease(current: str) -> bool:
    return bool(re.search(r"(dev|alpha|beta|rc|pre)", current, re.IGNORECASE))


def _origin(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    return f"{parsed.scheme}://{parsed.netloc}"


def _feed_url() -> str:
    configured = os.environ.get("OSTENSORY_UPDATE_FEED", "").strip()
    if configured:
        raw = configured
    else:
        raw = f"{DEFAULT_WEB}/{_repo()}/releases.atom"
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise UpdateError("更新订阅地址不正确。")
    if (
        parsed.scheme != "https"
        and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        and os.environ.get("OSTENSORY_UPDATE_ALLOW_HTTP") != "1"
    ):
        raise UpdateError("更新订阅必须使用 HTTPS；本机测试可显式设置 OSTENSORY_UPDATE_ALLOW_HTTP=1。")
    return raw


def _download_root(feed_url: str) -> str:
    configured = os.environ.get("OSTENSORY_UPDATE_DOWNLOAD_ROOT", "").strip()
    raw = configured or _origin(feed_url)
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise UpdateError("更新下载地址不正确。")
    if (
        parsed.scheme != "https"
        and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        and os.environ.get("OSTENSORY_UPDATE_ALLOW_HTTP") != "1"
    ):
        raise UpdateError("更新下载必须使用 HTTPS。")
    return raw.rstrip("/")


def _plain_text(value: str) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _atom_field(entry: ET.Element, name: str) -> str:
    node = entry.find(f"atom:{name}", ATOM_NS)
    return str(node.text or "").strip() if node is not None else ""


def _atom_tag(entry: ET.Element) -> tuple[str, str]:
    html_url = ""
    for link in entry.findall("atom:link", ATOM_NS):
        if (link.get("rel") or "alternate") == "alternate":
            html_url = str(link.get("href") or "").strip()
            break
    tag = ""
    if html_url:
        marker = "/releases/tag/"
        if marker in html_url:
            tag = urllib.parse.unquote(html_url.split(marker, 1)[1].split("?", 1)[0])
    if not tag:
        raw_id = _atom_field(entry, "id")
        if "/releases/tag/" in raw_id:
            tag = urllib.parse.unquote(raw_id.split("/releases/tag/", 1)[1])
    return tag, html_url


def _release_from_atom(
    tag: str,
    html_url: str,
    notes: str,
    published_at: str,
    download_root: str,
) -> Release:
    repo = _repo()
    version = normalize_version(tag)
    if version_key(version) is None:
        raise UpdateError("最新 Release 的版本号无法识别。")
    base = (
        f"{download_root}/{repo}/releases/download/"
        f"{urllib.parse.quote(tag, safe='')}"
    )
    checksum_url = f"{base}/{CHECKSUM_ASSET}"
    try:
        text = _http_get(checksum_url, limit=256 * 1024, accept="text/plain").decode(
            "utf-8", "replace"
        )
    except UpdateError as exc:
        if "还没有可用的 Release" in str(exc):
            raise UpdateError(
                "最新 Release 缺少 SHA256SUMS.txt，出于安全考虑拒绝自动更新。"
            ) from exc
        raise
    checksum = _checksum_from_text(text, ARCHIVE_ASSET)
    if not checksum:
        raise UpdateError("最新发布包没有 SHA256 校验值，出于安全考虑拒绝自动更新。")
    return Release(
        version=version,
        tag=tag,
        html_url=_safe_web_url(html_url),
        notes=notes[:12000],
        archive_url=f"{base}/{ARCHIVE_ASSET}",
        checksum=checksum,
        published_at=published_at,
        prerelease=_allow_prerelease(version),
    )


def _latest_release_from_feed(current: str) -> Release:
    """Use the public Releases Atom feed.

    The GitHub REST API is aggressively rate-limited for shared egress IPs.
    The Atom feed is a normal public web endpoint, needs no token, and gives us
    the tag/notes; release asset URLs are deterministic from the tag.  SHA256 is
    still downloaded and verified before installation.
    """
    feed_url = _feed_url()
    try:
        payload = _http_get(
            feed_url, limit=2 * 1024 * 1024, accept="application/atom+xml"
        )
    except UpdateError as exc:
        if "还没有可用的 Release" in str(exc):
            raise NoReleaseAvailable("仓库还没有可用的 Release。") from exc
        raise
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise UpdateError("更新订阅返回了无法识别的数据。") from exc
    allow_prerelease = _allow_prerelease(current)
    candidates: list[tuple[tuple[tuple[int, ...], int, str], str, str, str, str]] = []
    for entry in root.findall("atom:entry", ATOM_NS):
        tag, html_url = _atom_tag(entry)
        version = normalize_version(tag)
        key = version_key(version)
        if key is None:
            continue
        if _allow_prerelease(version) and not allow_prerelease:
            continue
        candidates.append(
            (
                key,
                tag,
                html_url,
                _plain_text(_atom_field(entry, "content")),
                _atom_field(entry, "updated"),
            )
        )
    if not candidates:
        raise NoReleaseAvailable("仓库还没有可用的 Release。")
    candidates.sort(key=lambda item: item[0], reverse=True)
    _, tag, html_url, notes, published_at = candidates[0]
    return _release_from_atom(
        tag, html_url, notes, published_at, _download_root(feed_url)
    )


def _latest_release_from_api(current: str) -> Release:
    """Legacy REST fallback for GitHub Enterprise or unusual mirrors."""
    allow_prerelease = _allow_prerelease(current)
    url = f"{_api_root()}/repos/{_repo()}/releases?per_page=100"
    payload = _get_json(url)
    if not isinstance(payload, list):
        raise UpdateError("更新服务器返回的 Release 列表格式不正确。")
    candidates: list[tuple[tuple[tuple[int, ...], int, str], dict[str, Any]]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        if item.get("draft"):
            continue
        if item.get("prerelease") and not allow_prerelease:
            continue
        version = normalize_version(str(item.get("tag_name") or ""))
        key = version_key(version)
        if key is not None:
            candidates.append((key, item))
    if not candidates:
        raise NoReleaseAvailable("仓库还没有可用的 Release。")
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    release = _release_from_payload(candidates[0][1])
    if release is None:
        raise UpdateError("最新 Release 没有可用的服务端安装包。")
    return release


def latest_release(current: str | None = None) -> Release:
    """Find the newest eligible release without relying on the REST API.

    Primary path is the public Atom feed plus deterministic release asset
    URLs.  The REST API remains as a fallback for GitHub Enterprise/custom
    mirrors, but a 403 there no longer prevents ordinary deployments from
    checking for updates.
    """
    current = normalize_version(current or current_version())
    try:
        return _latest_release_from_feed(current)
    except NoReleaseAvailable:
        raise
    except UpdateError as feed_error:
        if os.environ.get("OSTENSORY_UPDATE_API_ONLY") == "1":
            raise
        try:
            return _latest_release_from_api(current)
        except NoReleaseAvailable:
            raise
        except UpdateError as api_error:
            raise UpdateError(
                f"更新检查失败：{feed_error}（备用 API 也失败：{api_error}）"
            ) from api_error


def _parse_datetime(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _values(db: sqlite3.Connection, settings: dict[str, Any] | None = None) -> dict[str, str]:
    values = dict(UPDATE_DEFAULTS)
    if settings is not None:
        for key in UPDATE_SETTING_KEYS:
            if key in settings:
                values[key] = str(settings[key])
    else:
        for key in UPDATE_SETTING_KEYS:
            values[key] = get_setting(db, key, UPDATE_DEFAULTS[key])
    return values


def update_state(
    db: sqlite3.Connection,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    values = _values(db, settings)
    running = current_version()
    latest = values["update_latest_version"]
    installed = values["update_installed_version"]
    last_error = values["update_last_error"]
    last_check = _parse_datetime(values["update_last_check_at"])
    now = datetime.now(timezone.utc)
    check_age = (now - last_check).total_seconds() if last_check else None
    latest_valid = version_key(latest) is not None
    installed_valid = version_key(installed) is not None
    target = installed if installed_valid and is_newer(installed, running) else running
    available = bool(latest_valid and is_newer(latest, target))
    # The persisted flag is only a hint.  Once the process has restarted and its
# running version is no longer older than the installed version, the restart
# is complete even if the old flag is still present in the database.
    needs_restart = bool(installed_valid and is_newer(installed, running))
    current_is_newer = bool(latest_valid and is_newer(running, latest))
    try:
        repo = _repo()
    except UpdateError:
        repo = ""
    return {
        "current_version": running,
        "latest_version": latest,
        "latest_tag": values["update_latest_tag"],
        "latest_url": _safe_web_url(values["update_latest_url"]),
        "notes": values["update_latest_notes"],
        "installed_version": installed,
        "last_check_at": values["update_last_check_at"],
        "last_install_at": values["update_last_install_at"],
        "last_error": last_error,
        "backup_path": values["update_backup_path"],
        "auto_check": values["update_auto_check"] == "1",
        "auto_install": values["update_auto_install"] == "1",
        "available": available,
        "needs_restart": needs_restart,
        "current_is_newer": current_is_newer,
        "up_to_date": bool(
            latest_valid
            and not available
            and not needs_restart
            and not current_is_newer
        ),
        "check_age_seconds": check_age,
        "check_due": check_age is None or check_age >= CHECK_INTERVAL_SECONDS,
        "repo": repo,
        "deploy_mode": _deploy_mode(),
        "can_install": _can_install(),
    }


def _deploy_mode() -> str:
    configured = os.environ.get("OSTENSORY_UPDATE_DEPLOY_MODE", "").strip().lower()
    if configured in {"source", "docker"}:
        return configured
    if Path("/.dockerenv").exists():
        return "docker"
    return "source"


def _can_install() -> bool:
    return _deploy_mode() != "docker" or os.environ.get("OSTENSORY_UPDATE_ALLOW_DOCKER") == "1"


def check_for_update(
    db: sqlite3.Connection,
    *,
    force: bool = False,
) -> dict[str, Any]:
    state = update_state(db)
    if not force and not state["check_due"] and not state["last_error"]:
        # Cache successful checks.  A failed check is retried by the next
        # background tick (15 minutes), so a repaired feed/network does not
        # leave a stale error on the About page for six hours.
        return state
    try:
        release = latest_release(state["current_version"])
    except NoReleaseAvailable as exc:
        # No release yet is a normal state for a fresh repository, not an
        # error that should paint the About page red or retry aggressively.
        set_setting(db, "update_last_check_at", utc_now())
        set_setting(db, "update_last_error", "")
        set_setting(db, "update_latest_version", "")
        set_setting(db, "update_latest_tag", "")
        set_setting(db, "update_latest_url", "")
        set_setting(db, "update_latest_notes", "")
        db.commit()
        LOGGER.info("No eligible release available: %s", exc)
        return update_state(db)
    except UpdateError as exc:
        set_setting(db, "update_last_error", str(exc))
        set_setting(db, "update_last_check_at", utc_now())
        db.commit()
        raise
    set_setting(db, "update_last_check_at", utc_now())
    set_setting(db, "update_latest_version", release.version)
    set_setting(db, "update_latest_tag", release.tag)
    set_setting(db, "update_latest_url", release.html_url)
    set_setting(db, "update_latest_notes", release.notes)
    set_setting(db, "update_last_error", "")
    db.commit()
    return update_state(db)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_release(release: Release, destination: Path) -> None:
    payload = _http_get(
        release.archive_url,
        limit=MAX_ARCHIVE_BYTES,
        accept="application/gzip, application/octet-stream",
    )
    destination.write_bytes(payload)
    digest = _sha256_file(destination)
    if digest.lower() != release.checksum.lower():
        destination.unlink(missing_ok=True)
        raise UpdateError("发布包 SHA256 校验失败，已停止更新。")


def _safe_member_path(name: str) -> PurePosixPath | None:
    normalized = name.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        return None
    path = PurePosixPath(normalized)
    if any(part in {"", ".", ".."} for part in path.parts):
        return None
    if not path.parts:
        return None
    return path


def _extract_archive(archive: Path, destination: Path) -> Path:
    """Extract only the application payload, rejecting links and traversal."""
    destination.mkdir(parents=True, exist_ok=True)
    total = 0
    members = 0
    found_root: Path | None = None
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            members += 1
            if members > MAX_ARCHIVE_MEMBERS:
                raise UpdateError("发布包文件数量异常，已停止更新。")
            if member.issym() or member.islnk():
                raise UpdateError("发布包包含符号链接，已停止更新。")
            path = _safe_member_path(member.name)
            if path is None:
                raise UpdateError("发布包包含不安全的路径，已停止更新。")
            top = path.parts[0]
            if top not in {item.rstrip("/").split("/")[0] for item in CODE_ITEMS}:
                # Release tooling may include version metadata; ignore it.
                continue
            if member.isdir():
                target = destination.joinpath(*path.parts)
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise UpdateError("发布包包含不支持的文件类型，已停止更新。")
            total += int(member.size)
            if total > MAX_EXTRACTED_BYTES:
                raise UpdateError("发布包解压后超过大小限制，已停止更新。")
            target = destination.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(member)
            if source is None:
                raise UpdateError("发布包内容损坏，已停止更新。")
            with source, target.open("wb") as handle:
                shutil.copyfileobj(source, handle)
            if top == "app" and path.parts == ("app", "__init__.py"):
                found_root = target.parent
    if found_root is None or not (found_root / "main.py").is_file():
        raise UpdateError("发布包里找不到完整的 app 程序目录。")
    if not (destination / "run.py").is_file():
        raise UpdateError("发布包里缺少 run.py。")
    if not (destination / "requirements.txt").is_file():
        raise UpdateError("发布包里缺少 requirements.txt。")
    return destination


def _staged_version(stage: Path) -> str:
    init_file = stage / "app" / "__init__.py"
    try:
        text = init_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise UpdateError("无法读取新版本号。") from exc
    match = INIT_VERSION_RE.search(text)
    if not match:
        raise UpdateError("新版本程序缺少版本号。")
    return match.group(1).strip()


def _validate_python(stage: Path) -> None:
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "compileall", "-q", str(stage / "app")],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=120,
            check=False,
        )
        run_file = stage / "run.py"
        if completed.returncode == 0 and run_file.exists():
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import py_compile,sys; py_compile.compile(sys.argv[1], doraise=True)",
                    str(run_file),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=60,
                check=False,
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError(f"无法校验新版本程序：{exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stdout or "").strip().splitlines()[-6:]
        raise UpdateError("新版本程序语法校验失败，已停止更新。\n" + "\n".join(detail))
    for cache in (stage / "app").rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    cache_file = stage / "app" / "__pycache__"
    if cache_file.exists():
        shutil.rmtree(cache_file, ignore_errors=True)


def _write_database_snapshot(source_path: Path, target_path: Path) -> bool:
    if not source_path.exists():
        return False
    target_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(str(source_path), timeout=30)
    target = sqlite3.connect(str(target_path))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return True


def _copy_code(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for name in CODE_ITEMS:
        current = source / name
        if not current.exists():
            continue
        target = destination / name
        if current.is_dir():
            shutil.copytree(current, target, symlinks=False)
        else:
            shutil.copy2(current, target)


def _restore_code(server_dir: Path, backup_code: Path) -> None:
    for name in CODE_ITEMS:
        current = server_dir / name
        saved = backup_code / name
        if current.is_dir():
            shutil.rmtree(current)
        elif current.exists():
            current.unlink()
        if not saved.exists():
            continue
        target = server_dir / name
        if saved.is_dir():
            shutil.copytree(saved, target, symlinks=False)
        else:
            shutil.copy2(saved, target)


def _create_backup(config: Config, old_version: str, new_version: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = config.data_dir / "backups" / f"update-{old_version}-to-{new_version}-{stamp}"
    backup.mkdir(parents=True, exist_ok=False)
    _copy_code(config.server_dir, backup / "code-before")
    db_snapshot = backup / "ostensory.sqlite3"
    if _write_database_snapshot(config.db_path, db_snapshot):
        (backup / "database-snapshot.txt").write_text(
            "数据库快照由 SQLite backup API 生成，包含已提交的 WAL 内容。\n",
            encoding="utf-8",
        )
    for name in ("master.key", "secret.key"):
        path = config.data_dir / name
        if path.exists():
            shutil.copy2(path, backup / name)
    metadata = {
        "created_at": utc_now(),
        "old_version": old_version,
        "new_version": new_version,
        "data_dir": str(config.data_dir),
        "server_dir": str(config.server_dir),
    }
    (backup / "manifest.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return backup


def _requirements_changed(current: Path, staged: Path) -> bool:
    old = current / "requirements.txt"
    new = staged / "requirements.txt"
    return (old.read_bytes() if old.exists() else b"") != new.read_bytes()


def _install_requirements(requirements: Path) -> None:
    if os.environ.get("OSTENSORY_UPDATE_SKIP_PIP") == "1":
        return
    if not requirements.exists():
        return
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "-r",
        str(requirements),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=str(requirements.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=600,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError(f"安装新版本依赖失败：{exc}") from exc
    if completed.returncode != 0:
        tail = (completed.stdout or "").strip().splitlines()[-8:]
        detail = "\n".join(tail)
        raise UpdateError("安装新版本依赖失败。\n" + detail)


def _replace_directory(source: Path, target: Path, token: str) -> None:
    old = target.with_name(f".{target.name}.update-old-{token}")
    if old.exists():
        shutil.rmtree(old)
    moved = False
    if target.exists():
        os.replace(target, old)
        moved = True
    try:
        os.replace(source, target)
    except Exception:
        if moved and not target.exists():
            os.replace(old, target)
        raise
    if moved and old.exists():
        shutil.rmtree(old)


def _apply_stage(config: Config, stage: Path, token: str) -> None:
    server = config.server_dir
    # The app directory is the only critical tree.  Other tracked code files
    # are replaced only after it succeeds.
    _replace_directory(stage / "app", server / "app", token)
    for name in ("deploy",):
        staged = stage / name
        if staged.is_dir():
            try:
                _replace_directory(staged, server / name, token)
            except OSError:
                # deploy/ is documentation and examples; failing to update it
                # must not roll back a working application update.
                LOGGER.warning("Unable to replace %s during update", name)
    for name in ("run.py", "requirements.txt", "pyproject.toml", "README.md", "LICENSE"):
        staged = stage / name
        if not staged.is_file():
            continue
        target = server / name
        temp = server / f".{name}.update-new-{token}"
        shutil.copy2(staged, temp)
        os.replace(temp, target)


def _restart_command() -> list[str]:
    raw = os.environ.get("OSTENSORY_UPDATE_RESTART_CMD", "").strip()
    if not raw:
        return []
    try:
        import shlex

        return shlex.split(raw, posix=os.name != "nt")
    except ValueError:
        return []


def _run_restart_command() -> str:
    command = _restart_command()
    if not command:
        return ""
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=90,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"重启命令执行失败：{exc}"
    if completed.returncode != 0:
        tail = (completed.stdout or "").strip().splitlines()[-4:]
        return "重启命令返回失败：" + " / ".join(tail)
    return ""


def _lock_path(config: Config) -> Path:
    return config.data_dir / "update.lock"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        # On Windows os.kill(pid, 0) is not a reliable existence check.
        return True
    return True


def _lock_pid(path: Path) -> int:
    try:
        first = path.read_text(encoding="utf-8").splitlines()[0]
        return int(first.split("=", 1)[1])
    except (OSError, IndexError, ValueError):
        return 0


@contextmanager
def update_lock(config: Config) -> Iterator[None]:
    path = _lock_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    acquired = _LOCAL_UPDATE_LOCK.acquire(blocking=False)
    if not acquired:
        raise UpdateError("另一个更新任务正在进行，请稍后再试。")
    try:
        if path.exists():
            try:
                age = datetime.now(timezone.utc).timestamp() - path.stat().st_mtime
            except OSError:
                age = 0
            pid = _lock_pid(path)
            if age > 2 * 3600 or (pid and not _pid_alive(pid)):
                path.unlink(missing_ok=True)
            else:
                raise UpdateError("另一个更新任务正在进行，请稍后再试。")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"pid={os.getpid()}\nstarted={utc_now()}\n")
        try:
            yield
        finally:
            path.unlink(missing_ok=True)
    finally:
        _LOCAL_UPDATE_LOCK.release()


def install_release(
    config: Config,
    db: sqlite3.Connection,
    release: Release | None = None,
    *,
    restart: bool = False,
) -> InstallResult:
    old_version = current_version()
    with update_lock(config):
        release = release or latest_release(old_version)
        installed = get_setting(db, "update_installed_version", "")
        target = installed if version_key(installed) and is_newer(installed, old_version) else old_version
        if not is_newer(release.version, target):
            raise UpdateError(f"当前已经是 {target}，没有可安装的新版本。")
        if not _can_install():
            raise UpdateError(
                "当前是 Docker 部署，请拉取新镜像并重建容器；容器内自动更新无法持久保存。"
            )
        server = config.server_dir.resolve()
        try:
            data = config.data_dir.resolve()
            data.relative_to(server / "app")
        except ValueError:
            pass
        else:
            raise UpdateError("数据目录位于 app 内，拒绝自动更新以免覆盖数据。")

        try:
            stage_parent = Path(tempfile.mkdtemp(prefix=".ostensory-update-", dir=server))
        except OSError as exc:
            raise UpdateError(f"无法在程序目录创建更新临时目录：{exc}") from exc
        token = uuid.uuid4().hex[:12]
        archive = stage_parent / "release.tar.gz"
        stage = stage_parent / "stage"
        backup: Path | None = None
        try:
            _download_release(release, archive)
            _extract_archive(archive, stage)
            _validate_python(stage)
            staged_version = _staged_version(stage)
            if not versions_equal(staged_version, release.version):
                raise UpdateError(
                    f"发布包版本 {staged_version} 与 Release 版本 {release.version} 不一致，已停止更新。"
                )
            requirements_changed = _requirements_changed(server, stage)
            backup = _create_backup(config, old_version, release.version)
            if requirements_changed:
                _install_requirements(stage / "requirements.txt")
            _apply_stage(config, stage, token)
            set_setting(db, "update_installed_version", release.version)
            set_setting(db, "update_last_install_at", utc_now())
            set_setting(db, "update_needs_restart", "1")
            set_setting(db, "update_backup_path", str(backup))
            set_setting(db, "update_latest_version", release.version)
            set_setting(db, "update_latest_tag", release.tag)
            set_setting(db, "update_latest_url", release.html_url)
            set_setting(db, "update_latest_notes", release.notes)
            set_setting(db, "update_last_error", "")
            db.commit()
            if restart:
                restart_error = _run_restart_command()
                if restart_error:
                    set_setting(db, "update_last_error", restart_error)
                    db.commit()
            return InstallResult(
                old_version=old_version,
                new_version=release.version,
                backup_path=backup,
                requirements_changed=requirements_changed,
                restart_required=True,
            )
        except Exception as exc:
            if backup is not None:
                try:
                    _restore_code(server, backup / "code-before")
                except Exception:
                    LOGGER.exception("Rollback after failed update did not complete")
            if isinstance(exc, UpdateError):
                raise
            raise UpdateError(f"更新失败：{exc}") from exc
        finally:
            shutil.rmtree(stage_parent, ignore_errors=True)


def install_latest(
    config: Config,
    db: sqlite3.Connection,
    *,
    restart: bool = False,
) -> InstallResult:
    return install_release(
        config,
        db,
        latest_release(current_version()),
        restart=restart,
    )


def record_auto_error(db: sqlite3.Connection, exc: Exception) -> None:
    try:
        set_setting(db, "update_last_error", str(exc))
        db.commit()
    except sqlite3.Error:
        LOGGER.exception("Unable to record automatic update error")


def run_auto_update(config: Config) -> None:
    """Background check used by the web process.

    A code update cannot be imported into the already-running process, so it
    is installed on disk and flagged for restart.  Data/settings are untouched.
    """
    if config.testing or os.environ.get("OSTENSORY_UPDATE_DISABLED") == "1":
        return
    from .database import connect

    db = connect(config.db_path)
    try:
        if get_setting(db, "update_auto_check", UPDATE_DEFAULTS["update_auto_check"]) != "1":
            return
        try:
            state = check_for_update(db, force=False)
        except Exception as exc:
            record_auto_error(db, exc)
            return
        if not get_setting(db, "update_auto_install", "0") == "1":
            return
        if not state["available"] or not state["can_install"]:
            return
        try:
            install_release(
                config,
                db,
                latest_release(current_version()),
                restart=False,
            )
        except Exception as exc:
            record_auto_error(db, exc)
    finally:
        db.close()


def format_check_time(value: str) -> str:
    parsed = _parse_datetime(value)
    if not parsed:
        return "从未检查"
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M")


def backup_restore_hint(backup_path: str) -> str:
    if not backup_path:
        return ""
    return (
        "如需回滚，先停止服务，把备份目录中的 code-before 覆盖回程序目录，"
        "再用 ostensory.sqlite3 替换 data 目录中的数据库，然后启动服务。"
    )