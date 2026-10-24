from __future__ import annotations

import struct
import zlib
from pathlib import Path

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_TEXT_CHUNK = 32 * 1024 * 1024
MAX_TOTAL_TEXT = 16 * 1024 * 1024


def _decompress_limited(data: bytes, limit: int) -> tuple[bytes, bool]:
    try:
        dec = zlib.decompressobj()
        out = dec.decompress(data, limit)
        truncated = bool(dec.unconsumed_tail) or not dec.eof
        return out, truncated
    except zlib.error:
        return b"", True


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="replace")


def parse_png_text(path: Path) -> tuple[dict[str, str], list[str]]:
    """Read tEXt/zTXt/iTXt chunks without loading the image pixels."""
    texts: dict[str, str] = {}
    warnings: list[str] = []
    total = 0
    try:
        with path.open("rb") as handle:
            if handle.read(8) != PNG_SIGNATURE:
                return {}, ["不是有效的 PNG 文件"]
            while True:
                head = handle.read(8)
                if len(head) < 8:
                    break
                length, chunk_type = struct.unpack(">I4s", head)
                if length > MAX_TEXT_CHUNK:
                    warnings.append(f"跳过过大的 PNG 分块 {chunk_type!r}（{length} 字节）")
                    handle.seek(length + 4, 1)
                    if chunk_type == b"IEND":
                        break
                    continue
                payload = handle.read(length)
                handle.read(4)  # CRC
                if chunk_type == b"IEND":
                    break
                key: str | None = None
                value = b""
                if chunk_type == b"tEXt":
                    if b"\x00" not in payload:
                        continue
                    key_b, value = payload.split(b"\x00", 1)
                    key = key_b.decode("latin-1", errors="replace")
                elif chunk_type == b"zTXt":
                    if b"\x00" not in payload:
                        continue
                    key_b, rest = payload.split(b"\x00", 1)
                    if not rest:
                        continue
                    key = key_b.decode("latin-1", errors="replace")
                    value, truncated = _decompress_limited(rest[1:], MAX_TEXT_CHUNK)
                    if truncated:
                        warnings.append(f"zTXt {key} 解压不完整或超过上限")
                elif chunk_type == b"iTXt":
                    if b"\x00" not in payload:
                        continue
                    key_b, rest = payload.split(b"\x00", 1)
                    key = key_b.decode("latin-1", errors="replace")
                    if len(rest) < 2:
                        continue
                    compressed, method = rest[0], rest[1]
                    rest = rest[2:]
                    if b"\x00" not in rest:
                        continue
                    _lang, rest = rest.split(b"\x00", 1)
                    if b"\x00" not in rest:
                        continue
                    _translated, value = rest.split(b"\x00", 1)
                    if compressed and method == 0:
                        value, truncated = _decompress_limited(value, MAX_TEXT_CHUNK)
                        if truncated:
                            warnings.append(f"iTXt {key} 解压不完整或超过上限")
                else:
                    continue

                if not key:
                    continue
                total += len(value)
                if total > MAX_TOTAL_TEXT:
                    warnings.append("PNG 文本元数据总量超过上限，后续内容被忽略")
                    break
                if key in texts:
                    # ComfyUI normally writes unique keys; keep the first but report.
                    warnings.append(f"重复的 PNG 文本键：{key}")
                    continue
                texts[key] = _decode(value)
    except OSError as exc:
        warnings.append(f"读取 PNG 元数据失败：{exc}")
    return texts, warnings