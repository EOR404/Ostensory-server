from __future__ import annotations

import base64
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PREFIX = "enc:v1:"
AAD = b"ostensory-storage-config"
SECRET_FIELDS = ("password", "token", "secret_key")


def seal_secret(master_key: bytes, value: str) -> str:
    if not value:
        return ""
    nonce = os.urandom(12)
    ciphertext = AESGCM(master_key).encrypt(nonce, value.encode("utf-8"), AAD)
    return PREFIX + base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")


def open_secret(master_key: bytes, value: str) -> str:
    if not value or not value.startswith(PREFIX):
        # Older rows were stored as plaintext; keep them readable until the
        # next save, which writes the encrypted form.
        return value
    try:
        raw = base64.urlsafe_b64decode(value[len(PREFIX) :].encode("ascii"))
        nonce, ciphertext = raw[:12], raw[12:]
        return AESGCM(master_key).decrypt(nonce, ciphertext, AAD).decode("utf-8")
    except Exception as exc:
        raise ValueError("存储凭据无法解密，请检查 master.key 是否被替换") from exc


def encrypt_config(master_key: bytes, config: dict[str, Any]) -> dict[str, Any]:
    result = dict(config)
    for field in SECRET_FIELDS:
        value = result.get(field)
        if isinstance(value, str) and value and not value.startswith(PREFIX):
            result[field] = seal_secret(master_key, value)
    return result


def decrypt_config(master_key: bytes, config: dict[str, Any]) -> dict[str, Any]:
    result = dict(config)
    for field in SECRET_FIELDS:
        value = result.get(field)
        if isinstance(value, str) and value:
            result[field] = open_secret(master_key, value)
    return result