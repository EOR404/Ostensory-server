from __future__ import annotations

import json
import re
from typing import Any

from .comfy import _dedupe, _merge_loras, _num, _parse_lora_tags

META_LINE_RE = re.compile(r"^[ \t]*Steps\s*:\s*", re.IGNORECASE | re.MULTILINE)
KV_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9 _./-]*?)\s*:\s*(\"(?:[^\"\\]|\\.)*\"|[^,]*)(?:,|$)"
)


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    return value.replace('\\"', '"').replace("\\\\", "\\")


def _parse_meta_line(line: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for match in KV_RE.finditer(line):
        key = match.group(1).strip()
        value = _unquote(match.group(2))
        if key:
            result[key] = value
    return result


def _json_field(meta_text: str, name: str) -> Any:
    """Extract a JSON object/list whose commas would confuse the flat parser."""
    pattern = rf"\b{re.escape(name)}:\s*"
    match = re.search(pattern, meta_text, re.IGNORECASE)
    if not match:
        return None
    rest = meta_text[match.end() :].lstrip()
    if not rest or rest[0] not in "[{":
        return None
    opener = rest[0]
    closer = "]" if opener == "[" else "}"
    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(rest):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(rest[: index + 1])
                except (ValueError, TypeError):
                    return None
    return None


def _lora_from_hashes(meta_text: str) -> list[dict[str, Any]]:
    """Read LoRA names from Image-Saver/Civitai hashes and resources.

    ComfyUI-Image-Saver's default "easy remix" removes <lora:...> tags from the
    prompt, so the prompt alone is not enough to recover the LoRA list.
    """
    found: list[dict[str, Any]] = []
    resources = _json_field(meta_text, "Civitai resources")
    if isinstance(resources, list):
        for item in resources:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or item.get("modelType") or "").lower()
            if kind and "lora" not in kind:
                continue
            name = item.get("modelName") or item.get("name") or item.get("model_name")
            if not isinstance(name, str) or not name.strip():
                continue
            lora: dict[str, Any] = {"name": name.strip(), "source": "civitai"}
            weight = _num(item.get("weight"))
            if weight is not None:
                lora["model_weight"] = weight
            found.append(lora)

    hashes = _json_field(meta_text, "Hashes")
    if isinstance(hashes, dict):
        for key in hashes:
            name = str(key)
            if name.lower().startswith("lora:"):
                name = name.split(":", 1)[1].strip()
            elif "lora" not in name.lower():
                continue
            if name:
                found.append({"name": name, "source": "hashes"})

    lora_hashes = re.search(r"\bLora hashes:\s*\"([^\"]*)\"", meta_text, re.IGNORECASE)
    if lora_hashes:
        for part in lora_hashes.group(1).split(","):
            name = part.split(":", 1)[0].strip()
            if name:
                found.append({"name": name, "source": "lora_hashes"})
    return found


def parse_a1111(parameters: str) -> dict[str, Any] | None:
    text = (parameters or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return None
    warnings: list[str] = []
    positive = text
    negative = ""
    negative_match = re.search(r"^[ \t]*Negative prompt:[ \t]*", text, re.IGNORECASE | re.MULTILINE)
    if negative_match:
        positive = text[: negative_match.start()]
        remainder = text[negative_match.end() :]
        # The negative prompt ends at the metadata line.
        meta_match = META_LINE_RE.search(remainder)
        if meta_match:
            negative, meta_text = remainder[: meta_match.start()], remainder[meta_match.start() :]
        else:
            negative = remainder
            meta_text = ""
            warnings.append("未找到 A1111 参数行")
    else:
        meta_match = META_LINE_RE.search(text)
        if meta_match:
            positive = text[: meta_match.start()]
            meta_text = text[meta_match.start() :]
        else:
            meta_text = ""
            warnings.append("未找到 A1111 参数行，仅保留提示词")

    meta = _parse_meta_line(meta_text) if meta_text else {}
    fields: dict[str, Any] = {}
    key_map = {
        "steps": "steps",
        "sampler": "sampler_name",
        "schedule type": "scheduler",
        "cfg scale": "cfg",
        "seed": "seed",
        "denoising strength": "denoise",
        "clip skip": "clip_skip",
        "size": "size",
        "model": "model",
        "model hash": "model_hash",
        "vae": "vae",
    }
    for source_key, target in key_map.items():
        actual = next((key for key in meta if key.lower() == source_key), None)
        if not actual:
            continue
        raw_value = meta[actual]
        if target == "size":
            match = re.match(r"^\s*(\d+)\s*[xX]\s*(\d+)\s*$", raw_value)
            if match:
                fields["width"] = int(match.group(1))
                fields["height"] = int(match.group(2))
            fields["size"] = raw_value
        elif target == "sampler_name":
            fields[target] = raw_value
        elif target in {"steps", "seed", "cfg", "denoise", "clip_skip"}:
            value = _num(raw_value)
            if value is not None:
                fields[target] = value
        else:
            fields[target] = raw_value

    sampler = {key: fields[key] for key in ("steps", "cfg", "seed", "sampler_name", "scheduler", "denoise", "width", "height") if key in fields}
    sampler["class_type"] = "A1111"
    promotions = _dedupe([positive.strip(), negative.strip()])
    loras = _merge_loras(_parse_lora_tags(promotions), _lora_from_hashes(meta_text))
    resources = _json_field(meta_text, "Civitai resources")
    if not fields.get("model") and isinstance(resources, list):
        for item in resources:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or item.get("modelType") or "").lower()
            name = item.get("modelName") or item.get("name")
            if "lora" not in kind and isinstance(name, str) and name.strip():
                fields["model"] = name.strip()
                break
    checkpoints = [fields["model"]] if fields.get("model") else []
    vaes = [fields["vae"]] if fields.get("vae") else []
    return {
        "kind": "a1111",
        "positive_prompt": positive.strip(),
        "negative_prompt": negative.strip(),
        "prompts": promotions,
        "loras": loras,
        "checkpoints": checkpoints,
        "unets": [],
        "vaes": vaes,
        "base_model": checkpoints[0] if checkpoints else "",
        "clips": [],
        "controlnets": [],
        "embeddings": [],
        "samplers": [sampler],
        "width": fields.get("width"),
        "height": fields.get("height"),
        "node_count": 0,
        "nodes": [],
        "workflow_format": "a1111",
        "parameters": meta,
        "warnings": warnings,
    }