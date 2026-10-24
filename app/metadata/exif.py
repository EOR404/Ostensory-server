from __future__ import annotations

import struct
from pathlib import Path

from PIL import Image, UnidentifiedImageError


def _decode_exif_value(value, name: str = "") -> str:
    if isinstance(value, bytes):
        if value.startswith(b"UNICODE\x00"):
            return value[8:].decode("utf-16-le", errors="replace").strip("\x00")
        if value.startswith(b"ASCII\x00\x00\x00"):
            return value[8:].decode("ascii", errors="replace").strip("\x00")
        if value.startswith(b"JIS\x00\x00\x00\x00\x00"):
            return value[8:].decode("shift_jis", errors="replace").strip("\x00")
        if name == "XPComment":
            try:
                return value.decode("utf-16-le", errors="replace").strip("\x00")
            except UnicodeDecodeError:
                pass
        for encoding in ("utf-8", "utf-16-le", "latin-1"):
            try:
                text = value.decode(encoding).strip("\x00")
                if text:
                    return text
            except UnicodeDecodeError:
                continue
        return value.decode("latin-1", errors="replace").strip("\x00")
    if isinstance(value, str):
        return value.strip("\x00")
    return str(value)


def parse_jpeg_comments(path: Path) -> list[str]:
    """Read JPEG COM (0xFFFE) segments, where some tools store JSON metadata."""
    comments: list[str] = []
    try:
        with path.open("rb") as handle:
            if handle.read(2) != b"\xff\xd8":
                return comments
            while True:
                marker = handle.read(2)
                if len(marker) < 2 or marker[0] != 0xFF:
                    break
                code = marker[1]
                if code in (0xD8, 0xD9):
                    if code == 0xD9:
                        break
                    continue
                length_bytes = handle.read(2)
                if len(length_bytes) < 2:
                    break
                length = struct.unpack(">H", length_bytes)[0]
                if length < 2:
                    break
                payload = handle.read(length - 2)
                if code == 0xFE:
                    comments.append(payload.decode("utf-8", errors="replace"))
                if code == 0xDA:
                    break
    except OSError:
        pass
    return comments


def parse_exif_text(path: Path) -> tuple[dict[str, str], list[str]]:
    values: dict[str, str] = {}
    warnings: list[str] = []
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            sources = [exif]
            try:
                exif_ifd = exif.get_ifd(0x8769)
                if exif_ifd:
                    sources.append(exif_ifd)
            except (AttributeError, KeyError, ValueError):
                pass
            for source in sources:
                for tag_id, value in source.items():
                    name = Image.ExifTags.TAGS.get(tag_id, str(tag_id))
                    if name in {"ImageDescription", "UserComment", "XPComment", "Software", "Make", "Model"}:
                        text = _decode_exif_value(value, name)
                        if text:
                            values.setdefault(name, text)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        warnings.append(f"读取 EXIF 失败：{exc}")

    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        try:
            for index, comment in enumerate(parse_jpeg_comments(path)):
                if comment.strip():
                    values[f"JPEGComment{index}"] = comment
        except OSError:
            pass
    return values, warnings