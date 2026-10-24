from __future__ import annotations

import gzip
import ipaddress
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from starlette.requests import Request

from .config import Config

try:
    import maxminddb
except ImportError:  # pragma: no cover - optional at runtime
    maxminddb = None  # type: ignore[assignment]


def _is_trusted(ip: str, config: Config) -> bool:
    try:
        address = ipaddress.ip_address(ip.strip())
    except ValueError:
        return False
    return any(address in network for network in config.trusted_proxies)


def client_ip(request: Request, config: Config) -> str:
    peer = request.client.host if request.client else ""
    forwarded = request.headers.get("x-forwarded-for", "")
    if _is_trusted(peer, config):
        candidates = [part.strip() for part in forwarded.split(",") if part.strip()]
        if candidates:
            chain = candidates + [peer]
            for candidate in reversed(chain):
                if _is_trusted(candidate, config):
                    continue
                try:
                    ipaddress.ip_address(candidate)
                except ValueError:
                    continue
                return candidate
            return candidates[0]
        real = request.headers.get("x-real-ip", "").strip()
        if real:
            try:
                ipaddress.ip_address(real)
                return real
            except ValueError:
                pass
    return peer


def is_loopback(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


@dataclass
class GeoStatus:
    available: bool
    database: str
    error: str = ""
    overrides: int = 0


class GeoResolver:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.reader: Any = None
        self.error = ""
        self.overrides: list[tuple[ipaddress._BaseNetwork, str]] = []
        self._load()

    def _load(self) -> None:
        path = self.config.geoip_db_path
        if path and path.exists() and maxminddb is not None:
            try:
                self.reader = maxminddb.open_database(str(path))
            except Exception as exc:
                self.error = f"GeoIP 数据库读取失败：{exc}"
        elif path and not path.exists():
            self.error = f"GeoIP 数据库不存在：{path}"
        elif maxminddb is None:
            self.error = "未安装 maxminddb 模块"

        overrides_path = self.config.geoip_overrides_path
        if overrides_path and overrides_path.exists():
            try:
                data = json.loads(overrides_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    for cidr, country in data.items():
                        try:
                            network = ipaddress.ip_network(cidr, strict=False)
                        except ValueError:
                            continue
                        if isinstance(country, str) and len(country.strip()) == 2:
                            self.overrides.append((network, country.strip().upper()))
                    self.overrides.sort(key=lambda item: item[0].prefixlen, reverse=True)
            except (OSError, ValueError) as exc:
                self.error = f"GeoIP 覆盖文件读取失败：{exc}"

    def status(self) -> GeoStatus:
        return GeoStatus(
            available=self.reader is not None or bool(self.overrides),
            database=str(self.config.geoip_db_path or "未配置"),
            error=self.error,
            overrides=len(self.overrides),
        )

    def lookup(self, ip: str) -> tuple[str | None, str]:
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            return None, "invalid"
        for network, country in self.overrides:
            if address in network:
                return country, "override"
        if self.reader is None:
            return None, "unavailable"
        try:
            data = self.reader.get(ip)
        except Exception:
            return None, "error"
        if not isinstance(data, dict):
            return None, "not-found"
        country = (
            (data.get("country") or {}).get("iso_code")
            or (data.get("registered_country") or {}).get("iso_code")
            or (data.get("represented_country") or {}).get("iso_code")
        )
        if isinstance(country, str) and len(country) == 2:
            return country.upper(), "database"
        return None, "not-found"

    def country_for_request(self, request: Request, config: Config) -> tuple[str | None, str, str]:
        cached = getattr(request.state, "geo_country", None)
        if cached is not None:
            return cached
        ip = getattr(request.state, "client_ip", None) or client_ip(request, config)
        if config.allow_test_geo_header:
            override = request.headers.get("x-geo-country", "").strip().upper()
            if len(override) == 2 and override.isalpha():
                result = (override, "test-header", ip)
                request.state.geo_country = result
                return result
        country, source = self.lookup(ip)
        result = (country, source, ip)
        request.state.geo_country = result
        return result

    def close(self) -> None:
        if self.reader is not None:
            try:
                self.reader.close()
            except Exception:
                pass


def download_public_database(config: Config) -> tuple[bool, str]:
    """Download DB-IP Lite country MMDB (CC BY 4.0) atomically.

    Only this fixed, legal public source is contacted.  The caller must opt in
    in settings before invoking the admin action.  Older months are tried when
    the current month's file is not published yet.
    """
    if maxminddb is None:
        return False, "未安装 maxminddb 模块，无法校验数据库。"
    target = config.geoip_db_path or (config.data_dir / "geoip" / "dbip-country-lite.mmdb")
    target.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    months: list[tuple[int, int]] = []
    year, month = now.year, now.month
    for _ in range(3):
        months.append((year, month))
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    errors: list[str] = []
    for item_year, item_month in months:
        filename = f"dbip-country-lite-{item_year:04d}-{item_month:02d}.mmdb.gz"
        url = f"https://download.db-ip.com/free/{filename}"
        temp = target.with_suffix(target.suffix + ".download")
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "Ostensory/1.0 (+https://github.com/ostensory)"},
            )
            with urllib.request.urlopen(request, timeout=45) as response:
                payload = response.read(64 * 1024 * 1024 + 1)
            if len(payload) > 64 * 1024 * 1024:
                errors.append(f"{filename}: 文件过大")
                continue
            data = gzip.decompress(payload)
            if len(data) > 256 * 1024 * 1024:
                errors.append(f"{filename}: 解压后过大")
                continue
            temp.write_bytes(data)
            reader = maxminddb.open_database(str(temp))
            reader.close()
            os.replace(temp, target)
            try:
                target.chmod(0o600)
            except OSError:
                pass
            return True, f"已更新 {target.name}（来源：DB-IP Lite，CC BY 4.0）"
        except urllib.error.HTTPError as exc:
            errors.append(f"{filename}: HTTP {exc.code}")
        except Exception as exc:
            errors.append(f"{filename}: {exc}")
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
    detail = "；".join(errors[-3:]) if errors else "没有可下载的文件"
    return False, f"更新失败：{detail}"


def region_decision(
    request: Request,
    config: Config,
    geo: GeoResolver,
    mode: str,
    countries: list[str],
    fail_mode: str = "block",
) -> tuple[bool, str]:
    mode = (mode or "inherit").lower()
    countries = [c.upper() for c in countries if isinstance(c, str) and len(c) == 2]
    if mode not in {"allow", "block"} or not countries:
        return True, "no-rule"
    country, source, _ip = geo.country_for_request(request, config)
    if not country:
        # Fail closed for restricted content unless the operator explicitly chose allow.
        return fail_mode != "block", f"unknown-country:{source}"
    if mode == "block":
        return country not in countries, f"country:{country}"
    return country in countries, f"country:{country}"