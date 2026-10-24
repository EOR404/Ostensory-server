from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .storage import StorageError


class BackendError(StorageError):
    pass


def _clean_key(key: str) -> str:
    key = (key or "").strip().lstrip("/")
    if not key or ".." in key.split("/"):
        raise BackendError("非法存储路径")
    return key


def _clean_prefix(prefix: str) -> str:
    prefix = (prefix or "").strip().strip("/")
    return f"{prefix}/" if prefix else ""


class FileBackend:
    kind = ""

    def put_file(self, key: str, path: Path, content_type: str) -> None:
        raise NotImplementedError

    def get_to_file(self, key: str, path: Path) -> None:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError

    def exists(self, key: str) -> bool:
        raise NotImplementedError

    def test(self) -> str:
        raise NotImplementedError


class WorkerBackend(FileBackend):
    """Cloudflare Worker object gateway.

    The Worker exposes /api/storage/<key> with a Bearer token.  The same Worker
    also serves a small standalone gallery, but the server only uses this
    private object API.
    """

    kind = "worker"

    def __init__(self, config: dict[str, Any]) -> None:
        self.base_url = str(config.get("url") or "").strip().rstrip("/")
        self.token = str(config.get("token") or "").strip()
        self.prefix = _clean_prefix(str(config.get("prefix") or ""))
        self.verify_tls = bool(config.get("verify_tls", True))
        if not self.base_url or not self.token:
            raise BackendError("Worker 存储需要填写地址和 Token")

    def _url(self, key: str) -> str:
        return f"{self.base_url}/api/storage/{quote(self.prefix + _clean_key(key), safe='/')}"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def put_file(self, key: str, path: Path, content_type: str) -> None:
        with httpx.Client(timeout=120.0, verify=self.verify_tls) as client:
            with path.open("rb") as handle:
                response = client.put(
                    self._url(key),
                    content=handle,
                    headers={
                        **self._headers(),
                        "Content-Type": content_type or "application/octet-stream",
                    },
                )
        if response.status_code not in {200, 201, 204}:
            raise BackendError(f"Worker 上传失败：HTTP {response.status_code}")

    def get_to_file(self, key: str, path: Path) -> None:
        with httpx.Client(timeout=120.0, verify=self.verify_tls) as client:
            with client.stream("GET", self._url(key), headers=self._headers()) as response:
                if response.status_code != 200:
                    raise BackendError(f"Worker 读取失败：HTTP {response.status_code}")
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_name(path.name + ".part")
                with temp.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        handle.write(chunk)
                os.replace(temp, path)

    def delete(self, key: str) -> None:
        with httpx.Client(timeout=60.0, verify=self.verify_tls) as client:
            response = client.delete(self._url(key), headers=self._headers())
        if response.status_code not in {200, 204, 404}:
            raise BackendError(f"Worker 删除失败：HTTP {response.status_code}")

    def exists(self, key: str) -> bool:
        with httpx.Client(timeout=30.0, verify=self.verify_tls) as client:
            response = client.head(self._url(key), headers=self._headers())
        if response.status_code == 200:
            return True
        if response.status_code == 404:
            return False
        raise BackendError(f"Worker 检查失败：HTTP {response.status_code}")

    def test(self) -> str:
        with httpx.Client(timeout=30.0, verify=self.verify_tls) as client:
            health = client.get(f"{self.base_url}/api/health")
            if health.status_code != 200:
                raise BackendError(f"Worker 健康检查失败：HTTP {health.status_code}")
            body = health.json()
            probe = client.head(
                f"{self.base_url}/api/storage/__ostensory_health__",
                headers=self._headers(),
            )
        if probe.status_code in {401, 403}:
            raise BackendError("Worker Token 无效")
        if probe.status_code not in {200, 204, 404}:
            raise BackendError(f"Worker 对象接口异常：HTTP {probe.status_code}")
        return f"Worker 正常：{body.get('service', 'Ostensory worker')}"


class WebDAVBackend(FileBackend):
    kind = "webdav"

    def __init__(self, config: dict[str, Any]) -> None:
        self.base_url = str(config.get("url") or "").strip().rstrip("/")
        self.username = str(config.get("username") or "")
        self.password = str(config.get("password") or "")
        self.prefix = _clean_prefix(str(config.get("prefix") or ""))
        self.verify_tls = bool(config.get("verify_tls", True))
        if not self.base_url:
            raise BackendError("WebDAV 需要填写地址")

    def _url(self, key: str) -> str:
        return f"{self.base_url}/{quote(self.prefix + _clean_key(key), safe='/')}"

    def _auth(self) -> tuple[str, str] | None:
        if self.username or self.password:
            return (self.username, self.password)
        return None

    def _ensure_parents(self, client: httpx.Client, key: str) -> None:
        parts = (self.prefix + _clean_key(key)).split("/")[:-1]
        current: list[str] = []
        for part in parts:
            current.append(part)
            url = f"{self.base_url}/{quote('/'.join(current), safe='/')}/"
            response = client.request(
                "MKCOL", url, auth=self._auth(), follow_redirects=True
            )
            if response.status_code not in {200, 201, 204, 301, 302, 405}:
                raise BackendError(f"WebDAV 创建目录失败：HTTP {response.status_code}")

    def put_file(self, key: str, path: Path, content_type: str) -> None:
        with httpx.Client(timeout=120.0, verify=self.verify_tls) as client:
            self._ensure_parents(client, key)
            with path.open("rb") as handle:
                response = client.put(
                    self._url(key),
                    content=handle,
                    auth=self._auth(),
                    headers={"Content-Type": content_type or "application/octet-stream"},
                )
        if response.status_code not in {200, 201, 204}:
            raise BackendError(f"WebDAV 上传失败：HTTP {response.status_code}")

    def get_to_file(self, key: str, path: Path) -> None:
        with httpx.Client(timeout=120.0, verify=self.verify_tls) as client:
            with client.stream("GET", self._url(key), auth=self._auth()) as response:
                if response.status_code != 200:
                    raise BackendError(f"WebDAV 读取失败：HTTP {response.status_code}")
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_name(path.name + ".part")
                with temp.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        handle.write(chunk)
                os.replace(temp, path)

    def delete(self, key: str) -> None:
        with httpx.Client(timeout=60.0, verify=self.verify_tls) as client:
            response = client.delete(self._url(key), auth=self._auth())
        if response.status_code not in {200, 204, 404}:
            raise BackendError(f"WebDAV 删除失败：HTTP {response.status_code}")

    def exists(self, key: str) -> bool:
        with httpx.Client(timeout=30.0, verify=self.verify_tls) as client:
            response = client.head(self._url(key), auth=self._auth())
        if response.status_code == 200:
            return True
        if response.status_code == 404:
            return False
        raise BackendError(f"WebDAV 检查失败：HTTP {response.status_code}")

    def test(self) -> str:
        with httpx.Client(timeout=30.0, verify=self.verify_tls) as client:
            response = client.request(
                "PROPFIND",
                f"{self.base_url}/",
                auth=self._auth(),
                headers={"Depth": "0"},
            )
            if response.status_code not in {200, 207}:
                raise BackendError(f"WebDAV 连接失败：HTTP {response.status_code}")
        return "WebDAV 连接正常"


class S3Backend(FileBackend):
    """S3-compatible storage: AWS S3, Cloudflare R2, Alibaba OSS, MinIO."""

    kind = "s3"

    def __init__(self, config: dict[str, Any]) -> None:
        try:
            import boto3
            from botocore.config import Config as BotoConfig
        except ImportError as exc:  # pragma: no cover - dependency error surface
            raise BackendError("缺少 boto3，请先安装 server/requirements.txt") from exc
        self.bucket = str(config.get("bucket") or "").strip()
        self.prefix = _clean_prefix(str(config.get("prefix") or ""))
        endpoint = str(config.get("endpoint") or "").strip()
        region = str(config.get("region") or "auto").strip() or "auto"
        if not self.bucket:
            raise BackendError("S3 存储需要填写 Bucket")
        addressing = str(config.get("addressing_style") or "auto")
        kwargs: dict[str, Any] = {
            "aws_access_key_id": str(config.get("access_key") or ""),
            "aws_secret_access_key": str(config.get("secret_key") or ""),
            "region_name": region,
            "config": BotoConfig(
                signature_version="s3v4",
                s3={"addressing_style": addressing},
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        }
        if endpoint:
            kwargs["endpoint_url"] = endpoint
        self.client = boto3.client("s3", **kwargs)

    def _key(self, key: str) -> str:
        return self.prefix + _clean_key(key)

    def put_file(self, key: str, path: Path, content_type: str) -> None:
        extra = {"ContentType": content_type or "application/octet-stream"}
        self.client.upload_file(str(path), self.bucket, self._key(key), ExtraArgs=extra)

    def get_to_file(self, key: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".part")
        self.client.download_file(self.bucket, self._key(key), str(temp))
        os.replace(temp, path)

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._key(key))

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(key))
            return True
        except Exception as exc:
            code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise BackendError(f"S3 检查失败：{exc}") from exc

    def test(self) -> str:
        self.client.head_bucket(Bucket=self.bucket)
        return f"S3 存储正常：{self.bucket}"


def build_backend(kind: str, config: dict[str, Any]) -> FileBackend:
    if kind == "worker":
        return WorkerBackend(config)
    if kind == "webdav":
        return WebDAVBackend(config)
    if kind == "s3":
        return S3Backend(config)
    raise BackendError(f"不支持的存储类型：{kind}")