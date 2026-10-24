from __future__ import annotations

import ipaddress
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__

APP_VERSION = __version__


def _read_or_create_key(path: Path, nbytes: int = 32) -> bytes:
    if path.exists():
        data = path.read_bytes()
        if len(data) != nbytes:
            raise RuntimeError(f"密钥文件长度不正确：{path}（期望 {nbytes} 字节）")
        return data
    path.parent.mkdir(parents=True, exist_ok=True)
    data = secrets.token_bytes(nbytes)
    try:
        # O_EXCL avoids following a pre-created symlink or racing another
        # worker; the key is never readable by other users, even briefly.
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except FileExistsError:
        return _read_or_create_key(path, nbytes)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    return data


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_trusted_proxies(raw: str) -> list[ipaddress._BaseNetwork]:
    result: list[ipaddress._BaseNetwork] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            result.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return result


@dataclass
class Config:
    server_dir: Path
    data_dir: Path
    db_path: Path
    templates_dir: Path
    static_dir: Path
    secret_key: bytes
    master_key: bytes
    trusted_proxies: list[ipaddress._BaseNetwork] = field(default_factory=list)
    geoip_db_path: Path | None = None
    geoip_overrides_path: Path | None = None
    allow_test_geo_header: bool = False
    secure_cookies: str = "auto"
    session_days: int = 7
    testing: bool = False
    version: str = APP_VERSION

    @classmethod
    def from_env(
        cls,
        server_dir: Path | None = None,
        data_dir: Path | None = None,
        testing: bool = False,
    ) -> "Config":
        server_dir = Path(server_dir or Path(__file__).resolve().parents[1]).resolve()
        raw_data = data_dir or os.environ.get("OSTENSORY_DATA_DIR") or (server_dir / "data")
        data_dir = Path(raw_data).resolve()
        # A previous import may have been interrupted after moving files but
        # before the database/keys were complete.  Restore the pre-import copy
        # before any key is read or any new key is created.
        from .migration_recovery import recover_pending_migration

        recover_pending_migration(data_dir, server_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        try:
            # The database, originals and keys all live below this directory;
            # keep other local users out by default.
            data_dir.chmod(0o700)
        except OSError:
            pass
        (data_dir / "tmp").mkdir(parents=True, exist_ok=True)

        secret_path = data_dir / "secret.key"
        secret_env = os.environ.get("OSTENSORY_SECRET_KEY")
        if secret_env:
            secret_key = secret_env.encode("utf-8")
        else:
            secret_key = _read_or_create_key(secret_path)

        master_env = os.environ.get("OSTENSORY_MASTER_KEY")
        if master_env:
            try:
                master_key = bytes.fromhex(master_env.strip())
            except ValueError as exc:
                raise RuntimeError("OSTENSORY_MASTER_KEY 必须是 64 位十六进制字符串") from exc
            if len(master_key) != 32:
                raise RuntimeError("OSTENSORY_MASTER_KEY 必须是 32 字节（64 个 hex 字符）")
        else:
            master_key = _read_or_create_key(data_dir / "master.key")

        geoip_raw = os.environ.get("OSTENSORY_GEOIP_DB", "").strip()
        geoip_path = (
            Path(geoip_raw).resolve()
            if geoip_raw
            else data_dir / "geoip" / "dbip-country-lite.mmdb"
        )
        overrides_raw = os.environ.get("OSTENSORY_GEOIP_OVERRIDES", "").strip()
        overrides_path = (
            Path(overrides_raw).resolve()
            if overrides_raw
            else data_dir / "geoip" / "overrides.json"
        )

        trusted_raw = os.environ.get(
            "OSTENSORY_TRUSTED_PROXIES",
            "127.0.0.1/32,::1/128",
        )
        allow_test_header = _env_bool("OSTENSORY_ALLOW_TEST_GEO_HEADER", testing)

        return cls(
            server_dir=server_dir,
            data_dir=data_dir,
            db_path=data_dir / "ostensory.sqlite3",
            templates_dir=server_dir / "app" / "templates",
            static_dir=server_dir / "app" / "static",
            secret_key=secret_key,
            master_key=master_key,
            trusted_proxies=_parse_trusted_proxies(trusted_raw),
            geoip_db_path=geoip_path,
            geoip_overrides_path=overrides_path,
            allow_test_geo_header=allow_test_header,
            secure_cookies=os.environ.get("OSTENSORY_SECURE_COOKIES", "auto").lower(),
            session_days=int(os.environ.get("OSTENSORY_SESSION_DAYS", "7")),
            testing=testing,
        )