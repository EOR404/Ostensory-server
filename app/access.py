from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from starlette.requests import Request

from .config import Config
from .database import get_setting, utc_now
from .geo import GeoResolver, region_decision
from .security import current_user, unlock_scopes

SEGMENT_RE = re.compile(r"^[^/\\\x00-\x1f]{1,80}$")


def parse_countries(raw: str | list[str] | None) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        items = raw
    else:
        text = str(raw).strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                items = parsed
            else:
                items = text.replace(";", ",").split(",")
        except ValueError:
            items = text.replace(";", ",").split(",")
    out: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        code = item.strip().upper()
        if len(code) == 2 and code.isalpha() and code not in out:
            out.append(code)
    return out


def countries_json(raw: str | list[str] | None) -> str:
    return json.dumps(parse_countries(raw), ensure_ascii=False)


def validate_region_mode(mode: str | None) -> str:
    mode = (mode or "inherit").lower()
    return mode if mode in {"inherit", "allow", "block"} else "inherit"


@dataclass
class AlbumEffective:
    album_id: int | None
    nsfw: bool = False
    password_hash: str | None = None
    password_scope: str | None = None
    region_mode: str = "inherit"
    region_countries: list[str] = field(default_factory=list)
    region_source: str = "none"


@dataclass
class AccessDecision:
    allowed: bool
    reason: str = "ok"
    locked: bool = False
    lock_scope: str | None = None
    nsfw: bool = False


def album_chain(db: sqlite3.Connection, album_id: int | None) -> list[sqlite3.Row]:
    chain: list[sqlite3.Row] = []
    seen: set[int] = set()
    current = album_id
    while current and current not in seen and len(chain) < 32:
        seen.add(current)
        row = db.execute("SELECT * FROM albums WHERE id = ?", (current,)).fetchone()
        if not row:
            break
        chain.append(row)
        current = row["parent_id"]
    return chain


def album_effective(db: sqlite3.Connection, album_id: int | None) -> AlbumEffective:
    chain = album_chain(db, album_id)
    if not chain:
        return AlbumEffective(album_id=None)
    nsfw = any(bool(row["nsfw"]) for row in chain)
    password_hash = None
    password_scope = None
    region_mode = "inherit"
    region_countries: list[str] = []
    region_source = "none"
    for row in chain:
        if password_hash is None and row["access_password_hash"]:
            password_hash = row["access_password_hash"]
            password_scope = f"album:{row['id']}"
        if region_mode == "inherit" and row["region_mode"] in {"allow", "block"}:
            region_mode = row["region_mode"]
            region_countries = parse_countries(row["region_countries"])
            region_source = f"album:{row['id']}"
    if password_hash is None and chain[0]["box_id"]:
        box = db.execute(
            "SELECT id, access_password_hash FROM boxes WHERE id = ?",
            (chain[0]["box_id"],),
        ).fetchone()
        if box and box["access_password_hash"]:
            password_hash = box["access_password_hash"]
            password_scope = f"box:{box['id']}"
    return AlbumEffective(
        album_id=chain[0]["id"],
        nsfw=nsfw,
        password_hash=password_hash,
        password_scope=password_scope,
        region_mode=region_mode,
        region_countries=region_countries,
        region_source=region_source,
    )


def asset_effective(db: sqlite3.Connection, asset: sqlite3.Row) -> AlbumEffective:
    album = album_effective(db, asset["album_id"])
    nsfw = bool(asset["nsfw"]) or album.nsfw
    password_hash = asset["access_password_hash"] or album.password_hash
    password_scope = f"asset:{asset['id']}" if asset["access_password_hash"] else album.password_scope
    if password_hash is None and asset["box_id"]:
        box = db.execute(
            "SELECT id, access_password_hash FROM boxes WHERE id = ?",
            (asset["box_id"],),
        ).fetchone()
        if box and box["access_password_hash"]:
            password_hash = box["access_password_hash"]
            password_scope = f"box:{box['id']}"
    region_mode = album.region_mode
    region_countries = album.region_countries
    region_source = album.region_source
    if asset["region_mode"] in {"allow", "block"}:
        region_mode = asset["region_mode"]
        region_countries = parse_countries(asset["region_countries"])
        region_source = f"asset:{asset['id']}"
    if region_mode == "inherit" and nsfw:
        global_block = parse_countries(get_setting(db, "nsfw_blocked_countries", "CN"))
        if global_block:
            region_mode = "block"
            region_countries = global_block
            region_source = "global-nsfw"
    return AlbumEffective(
        album_id=asset["album_id"],
        nsfw=nsfw,
        password_hash=password_hash,
        password_scope=password_scope,
        region_mode=region_mode,
        region_countries=region_countries,
        region_source=region_source,
    )


def nsfw_region_effective(db: sqlite3.Connection, album_id: int | None) -> AlbumEffective:
    album = album_effective(db, album_id)
    if album.region_mode == "inherit" and album.nsfw:
        global_block = parse_countries(get_setting(db, "nsfw_blocked_countries", "CN"))
        if global_block:
            album.region_mode = "block"
            album.region_countries = global_block
            album.region_source = "global-nsfw"
    return album


def is_unlocked(request: Request, config: Config, scope: str | None) -> bool:
    if not scope:
        return True
    scopes = unlock_scopes(request, config)
    if scope in scopes:
        return True
    # An asset protected by a higher album is also unlocked by that album's ticket.
    if scope.startswith("album:"):
        return False
    return False


def check_album(
    request: Request,
    db: sqlite3.Connection,
    config: Config,
    geo: GeoResolver,
    album_id: int | None,
    admin: bool = False,
) -> AccessDecision:
    effective = nsfw_region_effective(db, album_id)
    if admin:
        return AccessDecision(True, "admin", False, None, effective.nsfw)
    if get_setting(db, "experimental_features", "0") == "1":
        fail_mode = get_setting(db, "geoip_fail_mode", "block")
        allowed, reason = region_decision(
            request, config, geo, effective.region_mode, effective.region_countries, fail_mode
        )
        if not allowed:
            return AccessDecision(False, "region", False, None, effective.nsfw)
    if effective.password_hash and not is_unlocked(request, config, effective.password_scope):
        return AccessDecision(False, "locked", True, effective.password_scope, effective.nsfw)
    return AccessDecision(True, "ok", False, None, effective.nsfw)


def check_box(
    request: Request,
    db: sqlite3.Connection,
    config: Config,
    box: sqlite3.Row | None,
    admin: bool = False,
) -> AccessDecision:
    if admin:
        return AccessDecision(True, "admin")
    if not box:
        return AccessDecision(False, "missing")
    password_hash = box["access_password_hash"] if "access_password_hash" in box.keys() else None
    if password_hash and not is_unlocked(request, config, f"box:{box['id']}"):
        return AccessDecision(False, "locked", True, f"box:{box['id']}", False)
    return AccessDecision(True, "ok", False, None, False)


def check_asset(
    request: Request,
    db: sqlite3.Connection,
    config: Config,
    geo: GeoResolver,
    asset: sqlite3.Row,
    admin: bool = False,
) -> AccessDecision:
    effective = asset_effective(db, asset)
    if admin:
        return AccessDecision(True, "admin", False, None, effective.nsfw)
    keys = asset.keys() if hasattr(asset, "keys") else asset
    viewer = current_user(db, request)
    owner_id = asset["owner_id"] if "owner_id" in keys else None
    is_owner = bool(viewer and owner_id and int(viewer["id"]) == int(owner_id))
    status = str(asset["status"] if "status" in keys else "approved")
    visibility = str(asset["visibility"] if "visibility" in keys else "public")
    if status != "approved" and not is_owner:
        return AccessDecision(False, "pending", False, None, effective.nsfw)
    if visibility == "private" and not is_owner:
        return AccessDecision(False, "private", False, None, effective.nsfw)
    if get_setting(db, "experimental_features", "0") == "1":
        fail_mode = get_setting(db, "geoip_fail_mode", "block")
        allowed, reason = region_decision(
            request, config, geo, effective.region_mode, effective.region_countries, fail_mode
        )
        if not allowed:
            return AccessDecision(False, "region", False, None, effective.nsfw)
    if effective.password_hash and not is_unlocked(request, config, effective.password_scope):
        return AccessDecision(False, "locked", True, effective.password_scope, effective.nsfw)
    return AccessDecision(True, "ok", False, None, effective.nsfw)


# ---------------------------------------------------------------------------
# Albums / paths
# ---------------------------------------------------------------------------


def clean_album_segments(path: str) -> list[str]:
    segments = []
    for raw in str(path or "").replace("\\", "/").split("/"):
        segment = raw.strip()
        if not segment:
            continue
        if segment in {".", ".."} or not SEGMENT_RE.match(segment):
            raise ValueError(f"非法的相册路径片段：{segment!r}")
        segments.append(segment)
    if len(segments) > 8:
        raise ValueError("相册层级不能超过 8 层")
    return segments


def ensure_album_path(db: sqlite3.Connection, path: str, create: bool = True) -> sqlite3.Row | None:
    segments = clean_album_segments(path)
    parent_id: int | None = None
    current: sqlite3.Row | None = None
    built: list[str] = []
    for segment in segments:
        built.append(segment)
        full_path = "/".join(built)
        row = db.execute("SELECT * FROM albums WHERE path = ?", (full_path,)).fetchone()
        if row:
            current = row
            parent_id = row["id"]
            continue
        if not create:
            return None
        now = utc_now()
        try:
            cursor = db.execute(
                "INSERT INTO albums(parent_id, name, path, description, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (parent_id, segment, full_path, "", now, now),
            )
            parent_id = int(cursor.lastrowid)
            current = db.execute("SELECT * FROM albums WHERE id = ?", (parent_id,)).fetchone()
        except sqlite3.IntegrityError:
            current = db.execute("SELECT * FROM albums WHERE path = ?", (full_path,)).fetchone()
            if not current:
                raise
            parent_id = current["id"]
    return current


def default_album(db: sqlite3.Connection) -> sqlite3.Row:
    path = get_setting(db, "default_album_path", "未分类") or "未分类"
    row = ensure_album_path(db, path, create=True)
    assert row is not None
    return row


def descendant_album_ids(db: sqlite3.Connection, album_id: int) -> list[int]:
    rows = db.execute(
        "WITH RECURSIVE tree(id) AS ("
        " SELECT id FROM albums WHERE id = ?"
        " UNION ALL SELECT a.id FROM albums a JOIN tree t ON a.parent_id = t.id"
        ") SELECT id FROM tree",
        (album_id,),
    ).fetchall()
    return [int(row["id"]) for row in rows]


def child_albums(db: sqlite3.Connection, album_id: int | None) -> list[sqlite3.Row]:
    if album_id is None:
        return db.execute(
            "SELECT * FROM albums WHERE parent_id IS NULL ORDER BY name COLLATE NOCASE"
        ).fetchall()
    return db.execute(
        "SELECT * FROM albums WHERE parent_id = ? ORDER BY name COLLATE NOCASE",
        (album_id,),
    ).fetchall()


def asset_query() -> str:
    return (
        "SELECT a.*, b.sha256, b.storage_key, b.thumb_key, b.preview_key, b.encrypted, "
        "b.size, b.mime, b.width, b.height, al.name AS album_name, al.path AS album_path, "
        "al.nsfw AS album_nsfw, al.box_id AS album_box_id, "
        "bx.name AS box_name, abx.name AS album_box_name, "
        "owner.display_name AS owner_display_name, owner.username AS owner_username, "
        "owner.role AS owner_role "
        "FROM assets a JOIN blobs b ON b.id = a.blob_id "
        "LEFT JOIN albums al ON al.id = a.album_id "
        "LEFT JOIN boxes bx ON bx.id = a.box_id "
        "LEFT JOIN boxes abx ON abx.id = al.box_id "
        "LEFT JOIN users owner ON owner.id = a.owner_id "
    )


def album_counts(db: sqlite3.Connection, album_id: int) -> tuple[int, int]:
    row = db.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(nsfw), 0) AS nsfw FROM assets "
        "WHERE album_id = ? AND deleted_at IS NULL",
        (album_id,),
    ).fetchone()
    return int(row["n"]), int(row["nsfw"])


def visible_album_counts(
    db: sqlite3.Connection, album_id: int, viewer_id: int | None = None
) -> tuple[int, int]:
    """Count images a particular visitor can actually open in an album.

    Rejected images are never counted.  Anonymous visitors and other users see
    approved public images only; the uploader additionally sees their own
    pending uploads so a pending cover never leads to an empty album page.
    """
    sql = (
        "SELECT COUNT(*) AS n, COALESCE(SUM(nsfw), 0) AS nsfw FROM assets "
        "WHERE album_id = ? AND deleted_at IS NULL AND "
        "((status = 'approved' AND visibility = 'public')"
    )
    params: list[object] = [album_id]
    if viewer_id:
        sql += " OR (owner_id = ? AND status = 'pending')"
        params.append(int(viewer_id))
    sql += ")"
    row = db.execute(sql, params).fetchone()
    return int(row["n"]), int(row["nsfw"])


def box_counts(db: sqlite3.Connection, box_id: int) -> tuple[int, int]:
    assets = db.execute(
        "SELECT COUNT(*) AS n FROM assets WHERE box_id = ? AND deleted_at IS NULL",
        (box_id,),
    ).fetchone()["n"]
    albums = db.execute(
        "SELECT COUNT(*) AS n FROM albums WHERE box_id = ?", (box_id,)
    ).fetchone()["n"]
    return int(assets), int(albums)


def box_cover(db: sqlite3.Connection, box_id: int) -> sqlite3.Row | None:
    box = db.execute("SELECT * FROM boxes WHERE id = ?", (box_id,)).fetchone()
    if not box:
        return None
    if box["cover_asset_id"]:
        asset = db.execute(
            asset_query() + "WHERE a.id = ? AND a.deleted_at IS NULL",
            (box["cover_asset_id"],),
        ).fetchone()
        if asset:
            return asset
    return db.execute(
        asset_query()
        + "WHERE a.box_id = ? AND a.deleted_at IS NULL ORDER BY a.id DESC LIMIT 1",
        (box_id,),
    ).fetchone()


def _cover_query(
    db: sqlite3.Connection,
    where: str,
    params: tuple[object, ...],
) -> sqlite3.Row | None:
    return db.execute(
        asset_query() + f"WHERE {where} ORDER BY a.id DESC LIMIT 1", params
    ).fetchone()


def album_cover(
    db: sqlite3.Connection, album_id: int, viewer_id: int | None = None
) -> sqlite3.Row | None:
    """Pick an album cover without ever preferring NSFW content.

    The order is deliberate: a suitable explicit cover, then a public
    non-NSFW image, then the uploader's pending non-NSFW image, and only after
    those a public NSFW image.  Rejected and private images are never eligible
    for a public cover.  Pending images are included only for their uploader.
    """
    album = db.execute("SELECT * FROM albums WHERE id = ?", (album_id,)).fetchone()
    if not album:
        return None
    base = (
        "a.album_id = ? AND a.deleted_at IS NULL "
        "AND a.status = 'approved' AND a.visibility = 'public'"
    )
    explicit_id = album["cover_asset_id"]
    if explicit_id:
        explicit = _cover_query(
            db,
            base + " AND a.id = ? AND a.nsfw = 0",
            (album_id, int(explicit_id)),
        )
        if explicit:
            return explicit
    public_clear = _cover_query(
        db, base + " AND a.nsfw = 0", (album_id,)
    )
    if public_clear:
        return public_clear
    if viewer_id:
        pending_base = (
            "a.album_id = ? AND a.deleted_at IS NULL "
            "AND a.status = 'pending' AND a.owner_id = ?"
        )
        pending_clear = _cover_query(
            db, pending_base + " AND a.nsfw = 0", (album_id, int(viewer_id))
        )
        if pending_clear:
            return pending_clear
    if explicit_id:
        explicit = _cover_query(
            db, base + " AND a.id = ?", (album_id, int(explicit_id))
        )
        if explicit:
            return explicit
    public_nsfw = _cover_query(db, base + " AND a.nsfw = 1", (album_id,))
    if public_nsfw:
        return public_nsfw
    if viewer_id:
        return _cover_query(
            db,
            "a.album_id = ? AND a.deleted_at IS NULL "
            "AND a.status = 'pending' AND a.owner_id = ? AND a.nsfw = 1",
            (album_id, int(viewer_id)),
        )
    return None


def touch_album(db: sqlite3.Connection, album_id: int | None) -> None:
    if album_id:
        db.execute("UPDATE albums SET updated_at = ? WHERE id = ?", (utc_now(), album_id))


def iso_timestamp(value: float | int | None) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (ValueError, OSError, OverflowError):
        return None