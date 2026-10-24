from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .a1111 import parse_a1111
from .comfy import parse_comfy
from .exif import parse_exif_text
from .png import parse_png_text

METADATA_VERSION = 1
MAX_RAW_BYTES = 8 * 1024 * 1024


def _try_json(value: str | None) -> Any:
    if not value or not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped or stripped[0] not in "{[":
        return None
    try:
        return json.loads(stripped)
    except (ValueError, TypeError):
        return None


def _looks_like_api_prompt(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and bool(value)
        and all(
            isinstance(node, dict) and ("class_type" in node or "inputs" in node)
            for node in value.values()
        )
    )


def _looks_like_ui_workflow(value: Any) -> bool:
    return isinstance(value, dict) and isinstance(value.get("nodes"), list)


def _text_map(path: Path) -> tuple[dict[str, str], list[str]]:
    suffix = path.suffix.lower()
    texts: dict[str, str] = {}
    warnings: list[str] = []
    if suffix == ".png":
        texts, warnings = parse_png_text(path)
    exif_texts, exif_warnings = parse_exif_text(path)
    warnings.extend(exif_warnings)
    for key, value in exif_texts.items():
        texts.setdefault(key, value)
        # ComfyUI-Image-Saver stores JPEG/WebP graph metadata as
        # "prompt:{json}" / "0:workflow:{json}" in EXIF Make/Model.
        if key in {"Make", "Model"}:
            for marker in ("prompt:", "workflow:"):
                if marker in value:
                    payload = value.split(marker, 1)[1].strip()
                    if payload:
                        texts.setdefault(marker[:-1], payload)
    return texts, warnings


def _pick(texts: dict[str, str], *names: str) -> str | None:
    lower = {key.lower(): value for key, value in texts.items()}
    for name in names:
        if name.lower() in lower:
            return lower[name.lower()]
    return None


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, dict):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict) and item.get("name"):
                out.append(str(item["name"]).strip())
        return out
    return []


def _flat_metadata(texts: dict[str, str]) -> dict[str, Any] | None:
    """Read the JSON flat metadata used by ComfyUI image-saver nodes.

    ComfyUI-Image-Saver / ComfyUI-ImageWithMetadata write a small object such as
    {"positive_prompt": ..., "model": ..., "steps": ...} under "user_metadata"
    or "parameters". It is often more reliable than tracing a custom graph.
    """
    for key, value in texts.items():
        parsed = _try_json(value)
        if not isinstance(parsed, dict):
            continue
        if not any(
            name in parsed
            for name in ("positive_prompt", "negative_prompt", "model", "sampler_name", "steps")
        ):
            continue

        prompt_value = parsed.get("positive_prompt") or parsed.get("prompt") or ""
        if not isinstance(prompt_value, str):
            prompt_value = ""
        positive = prompt_value.strip()
        negative_value = parsed.get("negative_prompt") or ""
        negative = negative_value.strip() if isinstance(negative_value, str) else ""
        if not positive and not negative:
            continue
        model = str(parsed.get("model") or parsed.get("checkpoint") or "").strip()
        unet = str(parsed.get("unet") or parsed.get("base_model") or "").strip()
        vae = str(parsed.get("vae") or "").strip()
        loras = _as_list(parsed.get("loras") or parsed.get("lora"))
        sampler: dict[str, Any] = {"class_type": "ComfyUI metadata"}
        for source, target in (
            ("seed", "seed"),
            ("steps", "steps"),
            ("cfg", "cfg"),
            ("sampler_name", "sampler_name"),
            ("scheduler", "scheduler"),
            ("denoise", "denoise"),
        ):
            if source in parsed:
                number = parsed[source] if source in {"sampler_name", "scheduler"} else _num_value(parsed[source])
                if number is not None and number != "":
                    sampler[target] = number
        checkpoints = [model] if model else []
        unets = [unet] if unet else []
        return {
            "kind": "comfyui",
            "positive_prompt": positive,
            "negative_prompt": negative,
            "prompts": [item for item in (positive, negative) if item],
            "loras": [{"name": name, "source": "metadata"} for name in loras],
            "checkpoints": checkpoints,
            "unets": unets,
            "vaes": [vae] if vae else [],
            "base_model": (checkpoints or unets or [""])[0],
            "embeddings": [],
            "samplers": [sampler] if len(sampler) > 1 else [],
            "width": parsed.get("width"),
            "height": parsed.get("height"),
            "node_count": 0,
            "nodes": [],
            "workflow_format": "metadata",
            "metadata": parsed,
            "warnings": [],
        }
    return None


def _merge_flat(result: dict[str, Any], flat: dict[str, Any]) -> None:
    if not result.get("positive_prompt"):
        result["positive_prompt"] = flat.get("positive_prompt", "")
    if not result.get("negative_prompt"):
        result["negative_prompt"] = flat.get("negative_prompt", "")
    result["prompts"] = list(
        dict.fromkeys(
            [str(item) for item in result.get("prompts", []) if str(item).strip()]
            + [str(item) for item in flat.get("prompts", []) if str(item).strip()]
        )
    )
    for field in ("checkpoints", "unets", "vaes"):
        merged = list(result.get(field) or [])
        for value in flat.get(field) or []:
            if value not in merged:
                merged.append(value)
        result[field] = merged
    known_loras = {
        str(item.get("name", "")).lower()
        for item in result.get("loras", [])
        if isinstance(item, dict)
    }
    for item in flat.get("loras") or []:
        if str(item.get("name", "")).lower() not in known_loras:
            result.setdefault("loras", []).append(item)
    if not result.get("base_model"):
        result["base_model"] = flat.get("base_model", "")
    if not result.get("samplers") and flat.get("samplers"):
        result["samplers"] = flat["samplers"]
    if not result.get("width"):
        result["width"] = flat.get("width")
    if not result.get("height"):
        result["height"] = flat.get("height")
    result["metadata"] = flat.get("metadata", {})


def _merge_a1111(result: dict[str, Any], parsed: dict[str, Any]) -> None:
    if not result.get("positive_prompt"):
        result["positive_prompt"] = parsed.get("positive_prompt", "")
    if not result.get("negative_prompt"):
        result["negative_prompt"] = parsed.get("negative_prompt", "")
    result["prompts"] = list(
        dict.fromkeys(
            [str(item) for item in result.get("prompts", []) if str(item).strip()]
            + [str(item) for item in parsed.get("prompts", []) if str(item).strip()]
        )
    )
    for field in ("checkpoints", "unets", "vaes"):
        merged = list(result.get(field) or [])
        for value in parsed.get(field) or []:
            if value not in merged:
                merged.append(value)
        result[field] = merged
    known_loras = {
        str(item.get("name", "")).lower()
        for item in result.get("loras", [])
        if isinstance(item, dict)
    }
    for item in parsed.get("loras") or []:
        if str(item.get("name", "")).lower() not in known_loras:
            result.setdefault("loras", []).append(item)
    if not result.get("base_model"):
        result["base_model"] = parsed.get("base_model", "")
    if not result.get("samplers") and parsed.get("samplers"):
        result["samplers"] = parsed["samplers"]
    if not result.get("width"):
        result["width"] = parsed.get("width")
    if not result.get("height"):
        result["height"] = parsed.get("height")


def _num_value(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            number = float(value)
            return int(number) if number.is_integer() else number
        except ValueError:
            return None
    return None


def parse_image_metadata(
    path: Path,
    width: int | None = None,
    height: int | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "version": METADATA_VERSION,
        "kind": "unknown",
        "positive_prompt": "",
        "negative_prompt": "",
        "prompts": [],
        "loras": [],
        "checkpoints": [],
        "unets": [],
        "vaes": [],
        "base_model": "",
        "clips": [],
        "controlnets": [],
        "embeddings": [],
        "samplers": [],
        "width": width,
        "height": height,
        "node_count": 0,
        "nodes": [],
        "warnings": [],
    }
    try:
        texts, warnings = _text_map(path)
    except Exception as exc:  # metadata must never block an upload
        result["warnings"].append(f"读取元数据失败：{exc}")
        return result
    result["warnings"].extend(warnings)
    flat = _flat_metadata(texts)

    prompt_text = _pick(
        texts,
        "prompt",
        "comfy_prompt",
        "comfyui_prompt",
        "prompt_json",
        "workflow_prompt",
    )
    workflow_text = _pick(texts, "workflow", "comfy_workflow", "comfyui_workflow", "workflow_json")
    parameters_text = _pick(
        texts,
        "parameters",
        "a1111_parameters",
        "generation_parameters",
        "sd_metadata",
    )
    comment_text = _pick(texts, "comment", "description", "imagedescription")
    user_comment = _pick(texts, "usercomment", "jpegcomment0")
    if not parameters_text and user_comment and (
        "steps:" in user_comment.lower() or "negative prompt:" in user_comment.lower()
    ):
        parameters_text = user_comment
    if not comment_text and user_comment:
        comment_text = user_comment

    # Some tools put the JSON in EXIF/JPEG comments without a "prompt" key.
    if not prompt_text and not workflow_text:
        for key, value in texts.items():
            parsed = _try_json(value)
            # Some exporters wrap both graphs in one JSON object.
            if isinstance(parsed, dict) and ("prompt" in parsed or "workflow" in parsed):
                wrapped_prompt = parsed.get("prompt")
                wrapped_workflow = parsed.get("workflow")
                prompt_candidate = (
                    wrapped_prompt if isinstance(wrapped_prompt, dict) else _try_json(wrapped_prompt)
                )
                workflow_candidate = (
                    wrapped_workflow
                    if isinstance(wrapped_workflow, dict)
                    else _try_json(wrapped_workflow)
                )
                if _looks_like_api_prompt(prompt_candidate):
                    prompt_text = json.dumps(prompt_candidate, ensure_ascii=False)
                if _looks_like_ui_workflow(workflow_candidate):
                    workflow_text = json.dumps(workflow_candidate, ensure_ascii=False)
                if prompt_text or workflow_text:
                    break
            if _looks_like_api_prompt(parsed):
                prompt_text = value
                break
            if _looks_like_ui_workflow(parsed):
                workflow_text = value
                break

    prompt_obj = _try_json(prompt_text)
    if prompt_obj is not None and not _looks_like_api_prompt(prompt_obj):
        result["warnings"].append("prompt 文本是 JSON，但不是 ComfyUI 节点格式")
        prompt_obj = None
    if prompt_text and prompt_obj is None:
        result["warnings"].append("prompt 不是有效的 JSON，已保留原文")
    workflow_obj = _try_json(workflow_text)
    if workflow_obj is not None and not _looks_like_ui_workflow(workflow_obj):
        result["warnings"].append("workflow 文本是 JSON，但不是 ComfyUI 工作流格式")
        workflow_obj = None
    if workflow_text and workflow_obj is None:
        result["warnings"].append("workflow 不是有效的 JSON，已保留原文")

    comfy = parse_comfy(prompt_obj, workflow_obj)
    if comfy:
        result.update(comfy)
        result["kind"] = "comfyui"
        result["version"] = METADATA_VERSION
        result["base_model"] = comfy.get("base_model") or next(
            iter(comfy.get("checkpoints") or comfy.get("unets") or []), ""
        )
        result["width"] = width or comfy.get("width")
        result["height"] = height or comfy.get("height")
        if flat:
            _merge_flat(result, flat)
        if parameters_text and not parameters_text.lstrip().startswith(("{", "[")):
            alt = parse_a1111(parameters_text)
            if alt:
                _merge_a1111(result, alt)
    elif flat:
        result.update(flat)
        result["version"] = METADATA_VERSION
        result["width"] = width or flat.get("width")
        result["height"] = height or flat.get("height")
    elif parameters_text:
        parsed = parse_a1111(parameters_text)
        if parsed:
            result.update(parsed)
            result["version"] = METADATA_VERSION
            result["base_model"] = parsed.get("base_model") or next(
                iter(parsed.get("checkpoints") or []), ""
            )
            result["width"] = width or parsed.get("width")
            result["height"] = height or parsed.get("height")
    else:
        if comment_text:
            parsed = parse_a1111(comment_text)
            if parsed:
                result.update(parsed)
                result["kind"] = "a1111"
                result["version"] = METADATA_VERSION
                result["base_model"] = parsed.get("base_model") or next(
                    iter(parsed.get("checkpoints") or []), ""
                )

    # Keep a human-usable prompt even when only an unknown "Description" exists.
    if result["kind"] == "unknown" and comment_text and not result["positive_prompt"]:
        result["positive_prompt"] = comment_text.strip()
        result["prompts"] = [comment_text.strip()]
        result["warnings"].append("只找到描述文本，未识别出生成参数")

    raw: dict[str, Any] = {}
    if prompt_obj is not None:
        raw["prompt"] = prompt_obj
    elif prompt_text:
        raw["prompt_text"] = prompt_text
    if workflow_obj is not None:
        raw["workflow"] = workflow_obj
    elif workflow_text:
        raw["workflow_text"] = workflow_text
    if parameters_text:
        raw["parameters"] = parameters_text
    if flat and flat.get("metadata"):
        raw["user_metadata"] = flat["metadata"]
    extra_texts = {
        key: value
        for key, value in texts.items()
        if key.lower()
        not in {
            "prompt",
            "comfy_prompt",
            "prompt_json",
            "workflow_prompt",
            "workflow",
            "comfy_workflow",
            "workflow_json",
            "parameters",
            "a1111_parameters",
            "generation_parameters",
            "comment",
            "description",
            "imagedescription",
        }
        and len(value) <= 1024 * 1024
    }
    if extra_texts:
        raw["text_chunks"] = extra_texts
    result["raw"] = raw
    result["source_keys"] = sorted(texts.keys())

    try:
        encoded = json.dumps(result, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > MAX_RAW_BYTES:
            result["raw"] = {"truncated": True}
            result["warnings"].append("原始元数据超过 8 MB，已只保留归一化摘要")
    except (TypeError, ValueError):
        result["raw"] = {"error": "原始元数据无法序列化"}
        result["warnings"].append("原始元数据无法序列化")
    return result


def _bounded_text(value: Any, limit: int) -> str:
    return str(value)[:limit] if value is not None else ""


def normalize_client_metadata(
    metadata: dict[str, Any],
    width: int | None = None,
    height: int | None = None,
) -> dict[str, Any]:
    """Validate and bound metadata parsed by a trusted sync client.

    The client sends only normalized fields (never the raw image bytes or the
    full PNG text chunks), so the server does not need to open and parse the
    upload again.  Length limits keep a compromised client from storing
    unbounded records in SQLite.
    """
    if not isinstance(metadata, dict):
        metadata = {}
    result: dict[str, Any] = {
        "version": 1,
        "kind": _bounded_text(metadata.get("kind") or "unknown", 40),
        "positive_prompt": _bounded_text(metadata.get("positive_prompt"), 200000),
        "negative_prompt": _bounded_text(metadata.get("negative_prompt"), 200000),
        "prompts": [],
        "loras": [],
        "checkpoints": [],
        "unets": [],
        "vaes": [],
        "base_model": _bounded_text(metadata.get("base_model"), 500),
        "clips": [],
        "controlnets": [],
        "embeddings": [],
        "samplers": [],
        "width": width,
        "height": height,
        "node_count": 0,
        "nodes": [],
        "warnings": [],
        "workflow_format": _bounded_text(metadata.get("workflow_format"), 40),
        "client_parsed": True,
    }
    for field in ("prompts",):
        values = metadata.get(field)
        if isinstance(values, list):
            result[field] = [
                _bounded_text(item, 20000)
                for item in values[:60]
                if isinstance(item, str) and item.strip()
            ]
    loras: list[dict[str, Any]] = []
    for item in metadata.get("loras") or []:
        if not isinstance(item, dict):
            continue
        name = _bounded_text(item.get("name"), 500).strip()
        if not name:
            continue
        record: dict[str, Any] = {"name": name}
        for field in ("model_weight", "clip_weight"):
            number = item.get(field)
            if isinstance(number, (int, float)) and not isinstance(number, bool):
                record[field] = number
        source = _bounded_text(item.get("source"), 40).strip()
        if source:
            record["source"] = source
        loras.append(record)
    result["loras"] = loras[:200]
    for field in ("checkpoints", "unets", "vaes", "clips", "controlnets", "embeddings"):
        values = metadata.get(field)
        if isinstance(values, list):
            result[field] = [
                _bounded_text(item, 500)
                for item in values[:100]
                if isinstance(item, (str, int, float)) and str(item).strip()
            ]
    samplers: list[dict[str, Any]] = []
    for item in metadata.get("samplers") or []:
        if not isinstance(item, dict):
            continue
        clean: dict[str, Any] = {}
        for key, value in list(item.items())[:40]:
            if isinstance(value, (str, int, float, bool)) and not isinstance(value, (list, dict)):
                clean[_bounded_text(key, 60)] = (
                    _bounded_text(value, 500) if isinstance(value, str) else value
                )
        if clean:
            samplers.append(clean)
    result["samplers"] = samplers[:50]
    if not result["width"]:
        try:
            candidate = int(metadata.get("width") or 0)
            result["width"] = candidate if 0 < candidate <= 100000 else None
        except (TypeError, ValueError):
            result["width"] = None
    if not result["height"]:
        try:
            candidate = int(metadata.get("height") or 0)
            result["height"] = candidate if 0 < candidate <= 100000 else None
        except (TypeError, ValueError):
            result["height"] = None
    try:
        node_count = int(metadata.get("node_count") or 0)
    except (TypeError, ValueError):
        node_count = 0
    result["node_count"] = max(0, min(node_count, 100000))
    nodes: list[dict[str, Any]] = []
    for item in metadata.get("nodes") or []:
        if not isinstance(item, dict):
            continue
        node = {
            "id": _bounded_text(item.get("id"), 80),
            "type": _bounded_text(item.get("type"), 200),
            "title": _bounded_text(item.get("title"), 300),
        }
        nodes.append(node)
    result["nodes"] = nodes[:200]
    result["warnings"] = [
        _bounded_text(item, 500)
        for item in (metadata.get("warnings") or [])[:50]
        if isinstance(item, str) and item.strip()
    ]
    return result


def client_metadata_payload(metadata: dict[str, Any]) -> dict[str, Any]:
    """Metadata safe to send from the desktop client to the sync API."""
    payload = normalize_client_metadata(metadata)
    payload.pop("client_parsed", None)
    payload.pop("raw", None)
    payload.pop("source_keys", None)
    return payload


def lora_names(metadata: dict[str, Any]) -> str:
    names = []
    for item in metadata.get("loras", []):
        if isinstance(item, dict) and item.get("name"):
            names.append(str(item["name"]))
    return ", ".join(dict.fromkeys(names))


def search_text(metadata: dict[str, Any]) -> str:
    parts = [
        str(metadata.get("positive_prompt", "")),
        str(metadata.get("negative_prompt", "")),
        lora_names(metadata),
        str(metadata.get("base_model", "")),
        " ".join(str(x) for x in metadata.get("checkpoints", [])),
        " ".join(str(x) for x in metadata.get("unets", [])),
        " ".join(str(x) for x in metadata.get("vaes", [])),
    ]
    return "\n".join(part for part in parts if part)


def workflow_for_download(metadata: dict[str, Any]) -> dict[str, Any] | None:
    raw = metadata.get("raw") if isinstance(metadata.get("raw"), dict) else {}
    workflow = raw.get("workflow")
    return workflow if isinstance(workflow, dict) else None