from __future__ import annotations

import json
import re
from typing import Any

LORA_TAG_RE = re.compile(
    r"<\s*lora\s*:\s*([^:>]+?)\s*"
    r"(?::\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*)?"
    r"(?::\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*)?>",
    re.IGNORECASE,
)
EMBEDDING_RE = re.compile(r"\bembedding\s*:\s*([^,\s]+)", re.IGNORECASE)
TEXT_KEYS = ("text", "text_g", "text_l", "clip_l", "t5xxl", "prompt", "positive_prompt")
MODEL_EXTENSIONS = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".sft")
TEXT_ENCODER_HINTS = ("textencode", "text_encode", "text encoder", "textencoder")
TEXT_NODE_HINTS = (
    "text",
    "string",
    "prompt",
    "primitive",
    "wildcard",
    "multiline",
    "positive",
    "negative",
)
UI_OPTION_VALUES = {
    "fixed",
    "increment",
    "decrement",
    "randomize",
    "enabled",
    "disabled",
    "true",
    "false",
    "none",
    "auto",
    "normal",
    "euler",
    "euler a",
    "dpmpp_2m",
    "dpmpp_2m_sde",
    "karras",
    "simple",
    "sgm_uniform",
    "exponential",
}
SKIP_TRACE_KEYS = {"model", "clip", "vae", "control_net", "image", "latent_image", "samples"}
LORA_RECORD_KEYS = {"name", "lora", "lora_name", "model_name"}


def _is_structured_json(value: str) -> bool:
    """True for token-editor / stack JSON, never for a normal prompt.

    Prompt-editor nodes often keep a human prompt plus a huge token-array
    snapshot in the same widgets.  Treating that snapshot as a prompt makes
    the detail page look like it contains several unrelated images.
    """
    text = (value or "").strip()
    if len(text) < 2 or text[0] not in "[{":
        return False
    if '"token_' in text[:200] or '"name"' in text[:200] or '"prompt"' in text[:200]:
        return True
    try:
        json.loads(text)
        return True
    except (TypeError, ValueError):
        return True


def _clean_text(value: Any, key: str = "", polarity: str | None = None) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if len(text) <= 1:
        return ""
    low_key = str(key).lower()
    if low_key in {"strength", "seed", "noise_seed", "sampler", "scheduler", "steps", "cfg"}:
        return ""
    if "strength" in low_key or "seed" in low_key:
        return ""
    if text.lower() in UI_OPTION_VALUES:
        return ""
    if _is_structured_json(text):
        return ""
    if text.startswith("打开") and len(text) < 40:
        return ""
    if polarity == "positive" and low_key.startswith("negative"):
        return ""
    if polarity == "negative" and low_key.startswith("positive"):
        return ""
    return text


def _ui_node_active(node: dict[str, Any]) -> bool:
    mode = node.get("mode", 0)
    if mode is None:
        return True
    try:
        return int(mode) == 0
    except (TypeError, ValueError):
        return str(mode).strip().lower() in {"", "0", "always"}


def _has_ref(inputs: dict[str, Any]) -> bool:
    return any(_is_ref(value) for value in inputs.values())


def _flag_on(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() not in {
            "",
            "0",
            "false",
            "off",
            "no",
            "none",
            "disabled",
            "muted",
            "bypass",
            "never",
        }
    return bool(value)


def _record_enabled(record: dict[str, Any]) -> bool:
    """Recognise the many enabled/disabled keys used by LoRA manager nodes."""
    for key in ("active", "enabled", "on", "switch"):
        if key in record and not _flag_on(record[key]):
            return False
    for key in ("disabled", "muted", "bypass", "hidden"):
        if key in record and _flag_on(record[key]):
            return False
    if "state" in record:
        state = str(record["state"]).strip().lower()
        if state in {"off", "disabled", "inactive", "muted", "bypass"}:
            return False
    if "mode" in record:
        mode = str(record["mode"]).strip().lower()
        if mode in {"2", "4", "off", "disabled", "muted", "bypass", "never"}:
            return False
    return True


def _suffix_enabled(values: dict[str, Any], suffix: str) -> bool:
    if not suffix:
        return True
    for prefix in ("enabled", "switch", "on", "active"):
        key = f"{prefix}_{suffix}"
        if key in values and not _flag_on(values[key]):
            return False
    for prefix in ("disabled", "muted", "bypass"):
        key = f"{prefix}_{suffix}"
        if key in values and _flag_on(values[key]):
            return False
    return True


def _has_lora_record(value: Any) -> bool:
    """Whether a widget value contains a structured LoRA-manager record."""
    if isinstance(value, list):
        return any(_has_lora_record(item) for item in value)
    if not isinstance(value, dict):
        return False
    return any(
        isinstance(value.get(key), str) and value[key].strip()
        for key in LORA_RECORD_KEYS
    )


def _lora_name_key(value: Any) -> str:
    return str(value or "").replace("\\", "/").rsplit("/", 1)[-1].strip().lower()


def _lora_record_names(value: Any) -> set[str]:
    """Collect every record name, including disabled ones, from a widget."""
    names: set[str] = set()
    if isinstance(value, list):
        for item in value:
            names.update(_lora_record_names(item))
        return names
    if not isinstance(value, dict):
        return names
    for key in LORA_RECORD_KEYS:
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            names.add(_lora_name_key(candidate))
            break
    for child in value.values():
        if isinstance(child, (dict, list)):
            names.update(_lora_record_names(child))
    return names


def _iter_lora_records(value: Any) -> list[dict[str, Any]]:
    """Find LoRA name/weight dictionaries in custom-node widget values."""
    found: list[dict[str, Any]] = []
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in "[{":
            try:
                parsed = json.loads(text)
            except (TypeError, ValueError):
                parsed = None
            if parsed is not None:
                found.extend(_iter_lora_records(parsed))
        return found
    if isinstance(value, list):
        for item in value:
            found.extend(_iter_lora_records(item))
        return found
    if not isinstance(value, dict):
        return found
    name = None
    for key in LORA_RECORD_KEYS:
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            name = candidate.strip()
            break
    if name:
        if not _record_enabled(value):
            return found
        item: dict[str, Any] = {"name": name, "source": "node"}
        weight = value.get("weight", value.get("strength", value.get("model_weight")))
        clip_weight = value.get("clipStrength", value.get("clip_weight"))
        number = _num(weight)
        clip_number = _num(clip_weight)
        if number is not None:
            item["model_weight"] = number
        if clip_number is not None:
            item["clip_weight"] = clip_number
        found.append(item)
    for key, item in value.items():
        if key in LORA_RECORD_KEYS:
            continue
        if isinstance(item, (dict, list, str)):
            found.extend(_iter_lora_records(item))
    return found


def _node_type(node: dict[str, Any]) -> str:
    return str(node.get("class_type") or node.get("type") or "")


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        value = str(value).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _num(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            number = float(value)
            return int(number) if number.is_integer() else number
        except ValueError:
            return None
    return None


def _looks_like_model(value: Any) -> bool:
    return isinstance(value, str) and value.lower().endswith(MODEL_EXTENSIONS)


def _is_ref(value: Any) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) >= 2
        and isinstance(value[0], (str, int))
        and str(value[0]).strip() != ""
    )


def _resolve_ref(value: Any, nodes: dict[str, dict[str, Any]]) -> tuple[str, dict[str, Any]] | None:
    if not _is_ref(value):
        return None
    node_id = str(value[0])
    node = nodes.get(node_id)
    if node is None:
        return None
    return node_id, node


def _extract_text_fields(node: dict[str, Any], polarity: str | None = None) -> list[str]:
    out: list[str] = []
    inputs = node.get("inputs")
    if not isinstance(inputs, dict):
        return out
    node_type = _node_type(node).lower()
    # Structured text-editor nodes expose the real prompt under a named key,
    # usually next to a large token JSON snapshot.  Prefer that key and never
    # concatenate every string in the node.
    preferred: list[str] = []
    if polarity:
        preferred.extend([polarity, f"{polarity}_prompt"])
    preferred.extend(
        ["text", "text_g", "text_l", "clip_l", "t5xxl", "prompt", "positive_prompt", "negative_prompt"]
    )
    for key in preferred:
        value = inputs.get(key)
        clean = _clean_text(value, key, polarity)
        if clean:
            out.append(clean)
    if out:
        return _dedupe(out)
    # Some custom text nodes use a differently named primitive. PrimitiveNode
    # (and a few front-end helpers) puts the actual string in "value".
    if any(hint in node_type for hint in TEXT_NODE_HINTS):
        candidates: list[str] = []
        for key, value in inputs.items():
            if not isinstance(value, str) or len(value.strip()) <= 2:
                continue
            if _looks_like_model(value) or key.lower() in {
                "clip",
                "model",
                "vae",
                "type",
                "device",
                "control_after_generate",
            }:
                continue
            clean = _clean_text(value, key, polarity)
            if clean:
                candidates.append(clean)
        if candidates:
            out.append(max(candidates, key=len))
    return _dedupe(out)
    return _dedupe(out)


def _trace_conditioning_text(
    node_id: str,
    nodes: dict[str, dict[str, Any]],
    visited: set[str],
    depth: int = 0,
    polarity: str | None = None,
) -> list[str]:
    if depth > 32 or len(visited) > 2000 or node_id in visited:
        return []
    visited.add(node_id)
    node = nodes.get(node_id)
    if not node:
        return []
    node_type = _node_type(node).lower()
    # ConditioningZeroOut deliberately removes the prompt; tracing its
    # upstream conditioning would make the negative prompt equal positive.
    if "zeroout" in node_type:
        return []
    if "mode" in node and not _ui_node_active(node):
        return []
    out = _extract_text_fields(node, polarity)
    inputs = node.get("inputs")
    if isinstance(inputs, dict):
        for key, value in inputs.items():
            if key in SKIP_TRACE_KEYS:
                continue
            resolved = _resolve_ref(value, nodes)
            if resolved:
                out.extend(
                    _trace_conditioning_text(
                        resolved[0], nodes, visited, depth + 1, polarity
                    )
                )
    return _dedupe(out)


def _sampler_prompt_refs(
    node: dict[str, Any],
    nodes: dict[str, dict[str, Any]],
) -> tuple[Any, Any, list[str]]:
    inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
    warnings: list[str] = []
    if "positive" in inputs or "negative" in inputs:
        return inputs.get("positive"), inputs.get("negative"), warnings
    if "guider" in inputs:
        resolved = _resolve_ref(inputs.get("guider"), nodes)
        if resolved:
            guider_inputs = resolved[1].get("inputs")
            if isinstance(guider_inputs, dict):
                if "positive" in guider_inputs or "negative" in guider_inputs:
                    return guider_inputs.get("positive"), guider_inputs.get("negative"), warnings
                if "conditioning" in guider_inputs:
                    return guider_inputs.get("conditioning"), None, warnings
        warnings.append("采样器使用 guider，但未找到 positive/negative 条件输入")
    if "conditioning" in inputs:
        return inputs.get("conditioning"), None, warnings
    return None, None, warnings


def _sampler_settings(node: dict[str, Any], nodes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
    result: dict[str, Any] = {"node_id": node.get("id") or "", "class_type": _node_type(node)}
    mapping = {
        "seed": ("seed", "noise_seed"),
        "steps": ("steps",),
        "cfg": ("cfg",),
        "sampler_name": ("sampler_name",),
        "scheduler": ("scheduler",),
        "denoise": ("denoise",),
        "start_at_step": ("start_at_step",),
        "end_at_step": ("end_at_step",),
    }
    for target, keys in mapping.items():
        for key in keys:
            if key in inputs:
                value = _num(inputs[key])
                if value is not None:
                    result[target] = value
                    break
    # SamplerCustomAdvanced keeps cfg on the guider node.
    if "cfg" not in result and isinstance(inputs.get("guider"), (list, tuple)):
        resolved = _resolve_ref(inputs.get("guider"), nodes)
        if resolved:
            guider_inputs = resolved[1].get("inputs")
            if isinstance(guider_inputs, dict):
                value = _num(guider_inputs.get("cfg"))
                if value is not None:
                    result["cfg"] = value
    return {key: value for key, value in result.items() if value not in (None, "")}


def _lora_from_inputs(node: dict[str, Any]) -> list[dict[str, Any]]:
    inputs = node.get("inputs")
    if not isinstance(inputs, dict):
        return []
    found: list[dict[str, Any]] = []
    node_type = _node_type(node).lower()

    def add(name: Any, model_weight: Any = None, clip_weight: Any = None, source: str = "node") -> None:
        if not isinstance(name, str) or not name.strip():
            return
        item: dict[str, Any] = {"name": name.strip(), "source": source}
        mw = _num(model_weight)
        cw = _num(clip_weight)
        if mw is not None:
            item["model_weight"] = mw
        if cw is not None:
            item["clip_weight"] = cw
        found.append(item)

    if isinstance(inputs.get("lora_name"), str):
        add(
            inputs.get("lora_name"),
            inputs.get("strength_model", inputs.get("model_strength")),
            inputs.get("strength_clip", inputs.get("clip_strength")),
        )
    for key, value in inputs.items():
        low = key.lower()
        if "temp" in low or "hidden" in low or "preview" in low:
            continue
        relevant = (
            low.startswith("lora")
            or low in {"loras", "lora_stack", "lora_list"}
            or ("lora" in node_type and low in {"text", "prompt", "value"})
        )
        if not relevant:
            continue
        if isinstance(value, str):
            if "<lora:" in value.lower():
                for item in _parse_lora_tags([value]):
                    found.append(item)
            elif _looks_like_model(value):
                suffix = low.replace("lora_name_", "").replace("lora_", "")
                if not _suffix_enabled(inputs, suffix):
                    continue
                strength = (
                    inputs.get(f"strength_{suffix}")
                    or inputs.get(f"strength_model_{suffix}")
                    or inputs.get(f"model_strength_{suffix}")
                )
                add(value, strength, inputs.get(f"clip_strength_{suffix}"))
            elif value.strip()[:1] in "[{":
                found.extend(_iter_lora_records(value))
        elif isinstance(value, (dict, list)):
            found.extend(_iter_lora_records(value))
    return found


def _lora_from_workflow_widgets(node: dict[str, Any]) -> list[dict[str, Any]]:
    node_type = _node_type(node).lower()
    if "lora" not in node_type or not _ui_node_active(node):
        return []
    # A detached Lora Loader is present in many saved workflows but is not in
    # the active model chain.  Do not report it as a used LoRA.
    inputs = node.get("inputs")
    if isinstance(inputs, list):
        linked = [item for item in inputs if isinstance(item, dict) and "link" in item]
        # Widget-only nodes (for example an older UI export) have no ``link``
        # keys at all and are still meaningful.  A node that does expose link
        # sockets but whose model/clip sockets are all empty is detached.
        if linked and not any(item.get("link") is not None for item in linked):
            return []
    found: list[dict[str, Any]] = []
    items = _ui_widget_items(node)
    widget_map = {str(key): value for key, value in items}

    # Lora Manager nodes keep two copies of their state: a human-readable
    # ``<lora:...>`` text widget that includes every entry (enabled *and*
    # disabled), and a structured record list with an ``active`` flag.  The
    # record list is authoritative; parsing the text widget would list LoRAs
    # that were switched off when the image was generated.
    record_values: list[Any] = []
    for _key, value in items:
        # The record list can be nested inside a wrapper such as
        # ``[{"version": 1, ...}, "<lora:...>", [{...records...}]]``.
        if _has_lora_record(value):
            record_values.append(value)
    if record_values:
        return _iter_lora_records(record_values)

    for key, value in items:
        low_key = str(key).lower()
        if any(word in low_key for word in ("temp", "hidden", "preview")):
            continue
        if isinstance(value, str):
            if "<lora:" in value.lower():
                found.extend(_parse_lora_tags([value]))
            elif value.strip()[:1] in "[{":
                found.extend(_iter_lora_records(value))
            elif _looks_like_model(value):
                suffix = low_key.replace("lora_name_", "").replace("lora_", "")
                if not _suffix_enabled(widget_map, suffix):
                    continue
                item: dict[str, Any] = {"name": value.strip(), "source": "workflow"}
                found.append(item)
        elif isinstance(value, (dict, list)):
            found.extend(_iter_lora_records(value))
    # Attach widget weights only when a single ordinary LoraLoader is present.
    if len(found) == 1 and "weight" not in found[0] and "model_weight" not in found[0]:
        numbers = [
            value
            for _key, value in items
            if isinstance(value, (int, float))
            and not isinstance(value, bool)
            and "seed" not in str(_key).lower()
        ]
        if numbers:
            found[0]["model_weight"] = _num(numbers[0])
        if len(numbers) >= 2:
            found[0]["clip_weight"] = _num(numbers[1])
    return found


def _display_prompt(text: str) -> str:
    """Remove LoRA tags that are already shown as a separate LoRA list."""
    cleaned = LORA_TAG_RE.sub("", text or "")
    cleaned = re.sub(r"[ \t]*,([ \t]*,)+", ",", cleaned)
    cleaned = re.sub(r"[ \t]*,[ \t]*$", "", cleaned)
    return cleaned.strip()


def _parse_lora_tags(texts: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for text in texts:
        for match in LORA_TAG_RE.finditer(text or ""):
            name = match.group(1).strip()
            if not name:
                continue
            weight = _num(match.group(2)) if match.group(2) is not None else None
            clip_weight = _num(match.group(3)) if match.group(3) is not None else None
            item: dict[str, Any] = {"name": name, "source": "text"}
            if weight is not None:
                item["model_weight"] = weight
            if clip_weight is not None:
                item["clip_weight"] = clip_weight
            out.append(item)
    return out


def _merge_loras(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for group in groups:
        for item in group:
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            key = name.lower()
            current = merged.setdefault(key, {"name": name, "source": item.get("source", "node")})
            for field in ("model_weight", "clip_weight"):
                if item.get(field) is not None and current.get(field) is None:
                    current[field] = item[field]
    return list(merged.values())


def _collect_models(nodes: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    result = {"checkpoints": [], "unets": [], "vaes": [], "clips": [], "controlnets": []}
    for node in nodes.values():
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        node_type = _node_type(node).lower()
        for key, target in (
            ("ckpt_name", "checkpoints"),
            ("unet_name", "unets"),
            ("vae_name", "vaes"),
            ("clip_name", "clips"),
            ("control_net_name", "controlnets"),
        ):
            value = inputs.get(key)
            if isinstance(value, str) and value.strip():
                result[target].append(value.strip())
        if not result["checkpoints"] and "checkpoint" in node_type:
            for value in inputs.values():
                if _looks_like_model(value):
                    result["checkpoints"].append(str(value))
    return {key: _dedupe(values) for key, values in result.items()}


def _base_model_name(checkpoints: list[str], unets: list[str]) -> str:
    for value in [*checkpoints, *unets]:
        if value:
            return str(value)
    return ""


def _dimensions(nodes: dict[str, dict[str, Any]]) -> tuple[int | None, int | None]:
    ordered = sorted(
        nodes.values(),
        key=lambda node: (
            0
            if any(hint in _node_type(node).lower() for hint in ("emptylatent", "latent", "empty"))
            else 1
        ),
    )
    for node in ordered:
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        width, height = _num(inputs.get("width")), _num(inputs.get("height"))
        if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
            return width, height
    return None, None


def parse_api_prompt(prompt: dict[str, Any]) -> dict[str, Any]:
    nodes = {str(k): v for k, v in prompt.items() if isinstance(v, dict)}
    warnings: list[str] = []
    positive: list[str] = []
    negative: list[str] = []
    samplers: list[dict[str, Any]] = []
    sampler_nodes: list[dict[str, Any]] = []

    for node_id, node in nodes.items():
        node_type = _node_type(node)
        low = node_type.lower()
        if "select" in low:
            continue
        if "ksampler" in low or low in {
            "samplercustom",
            "samplercustomadvanced",
            "samplercustomadvancedsdxl",
        }:
            sampler_nodes.append((node_id, node))

    for node_id, node in sampler_nodes:
        pos_ref, neg_ref, local_warnings = _sampler_prompt_refs(node, nodes)
        warnings.extend(local_warnings)
        if pos_ref:
            resolved = _resolve_ref(pos_ref, nodes)
            if resolved:
                positive.extend(
                    _trace_conditioning_text(resolved[0], nodes, set(), polarity="positive")
                )
            elif isinstance(pos_ref, str):
                positive.append(pos_ref)
        if neg_ref:
            resolved = _resolve_ref(neg_ref, nodes)
            if resolved:
                negative.extend(
                    _trace_conditioning_text(resolved[0], nodes, set(), polarity="negative")
                )
            elif isinstance(neg_ref, str):
                negative.append(neg_ref)
        settings = _sampler_settings(node, nodes)
        settings["node_id"] = node_id
        samplers.append(settings)

    if not sampler_nodes:
        text_nodes = [
            node
            for node in nodes.values()
            if any(hint in _node_type(node).lower() for hint in TEXT_ENCODER_HINTS)
        ]
        for node in text_nodes:
            positive.extend(_extract_text_fields(node))
        if text_nodes:
            warnings.append("未找到采样器节点，已将文本编码节点中的文本作为正向提示词")

    all_text = _dedupe(positive + negative)
    connected_lora_items: list[dict[str, Any]] = []
    api_record_names: set[str] = set()
    for node in nodes.values():
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        low_type = _node_type(node).lower()
        lora_node = "lora" in low_type or any(
            str(key).lower().startswith("lora") for key in inputs
        )
        # Only LoRAs that participate in the model/clip chain are used.  A
        # detached loader still appears in a saved graph but does not affect
        # the rendered image.
        if lora_node and _has_ref(inputs):
            connected_lora_items.extend(_lora_from_inputs(node))
            for value in inputs.values():
                api_record_names.update(_lora_record_names(value))
    api_tags = _parse_lora_tags(all_text)
    if api_record_names:
        # LoraManager keeps disabled entries in its text widget.  Structured
        # records with ``active`` flags win; tags only cover LoRAs that the
        # manager did not record at all.
        api_tags = [
            item for item in api_tags if _lora_name_key(item.get("name")) not in api_record_names
        ]
        connected_lora_items = [
            item
            for item in connected_lora_items
            if item.get("source") != "text"
            or _lora_name_key(item.get("name")) not in api_record_names
        ]
    loras = _merge_loras(connected_lora_items, api_tags)
    models = _collect_models(nodes)
    width, height = _dimensions(nodes)
    for sampler in samplers:
        if width and height:
            sampler.setdefault("width", width)
            sampler.setdefault("height", height)

    embeddings = _dedupe(
        match.group(1)
        for text in all_text
        for match in EMBEDDING_RE.finditer(text)
    )
    node_summary = [
        {
            "id": str(node_id),
            "type": _node_type(node),
            "title": (node.get("_meta") or {}).get("title", "") if isinstance(node.get("_meta"), dict) else "",
        }
        for node_id, node in nodes.items()
    ]
    return {
        "positive_prompt": _display_prompt("\n\n".join(_dedupe(positive))),
        "negative_prompt": _display_prompt("\n\n".join(_dedupe(negative))),
        "prompts": all_text,
        "loras": loras,
        "checkpoints": models["checkpoints"],
        "unets": models["unets"],
        "vaes": models["vaes"],
        "clips": models["clips"],
        "controlnets": models["controlnets"],
        "base_model": _base_model_name(models["checkpoints"], models["unets"]),
        "embeddings": embeddings,
        "samplers": samplers,
        "width": width,
        "height": height,
        "node_count": len(nodes),
        "nodes": node_summary,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Web UI workflow format
# ---------------------------------------------------------------------------


def _ui_nodes(workflow: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw_nodes = workflow.get("nodes")
    if not isinstance(raw_nodes, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for node in raw_nodes:
        if isinstance(node, dict) and node.get("id") is not None:
            result[str(node["id"])] = node
    return result


def _ui_links(workflow: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    raw = workflow.get("links")
    if not isinstance(raw, list):
        return result
    for link in raw:
        if isinstance(link, list) and len(link) >= 5:
            result[str(link[0])] = {
                "id": str(link[0]),
                "origin_id": str(link[1]),
                "origin_slot": link[2],
                "target_id": str(link[3]),
                "target_slot": link[4],
                "type": link[5] if len(link) > 5 else "",
            }
        elif isinstance(link, dict) and link.get("id") is not None:
            result[str(link["id"])] = {
                "id": str(link["id"]),
                "origin_id": str(link.get("origin_id")),
                "origin_slot": link.get("origin_slot"),
                "target_id": str(link.get("target_id")),
                "target_slot": link.get("target_slot"),
                "type": link.get("type", ""),
            }
    return result


def _ui_ancestor_ids(
    start_ids: list[str],
    nodes: dict[str, dict[str, Any]],
    links: dict[str, dict[str, Any]],
) -> set[str]:
    """All nodes upstream of the active samplers, including model/CLIP chains."""
    seen: set[str] = set()
    stack = [str(item) for item in start_ids]
    while stack and len(seen) < 4000:
        node_id = stack.pop()
        if node_id in seen:
            continue
        seen.add(node_id)
        node = nodes.get(node_id)
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, list):
            continue
        for item in inputs:
            if not isinstance(item, dict) or item.get("link") is None:
                continue
            link = links.get(str(item["link"]))
            if link and link.get("origin_id") not in seen:
                stack.append(str(link["origin_id"]))
    return seen


def _ui_input_link(node: dict[str, Any], name: str) -> str | None:
    inputs = node.get("inputs")
    if not isinstance(inputs, list):
        return None
    for item in inputs:
        if isinstance(item, dict) and item.get("name") == name and item.get("link") is not None:
            return str(item["link"])
    return None


def _ui_widget_items(node: dict[str, Any]) -> list[tuple[str, Any]]:
    """Return widget values from both old list and newer input-widget formats."""
    items: list[tuple[str, Any]] = []
    values = node.get("widgets_values")
    if isinstance(values, list):
        items.extend((str(index), value) for index, value in enumerate(values))
    elif isinstance(values, dict):
        items.extend((str(key), value) for key, value in values.items())
    inputs = node.get("inputs")
    if isinstance(inputs, list):
        for item in inputs:
            if not isinstance(item, dict):
                continue
            widget = item.get("widget")
            if isinstance(widget, dict) and "value" in widget:
                key = str(widget.get("name") or item.get("name") or "")
                items.append((key, widget["value"]))
            elif "value" in item:
                items.append((str(item.get("name") or ""), item["value"]))
    return items


def _ui_widget_strings(node: dict[str, Any]) -> list[str]:
    return [
        str(value).strip()
        for _key, value in _ui_widget_items(node)
        if isinstance(value, str) and str(value).strip()
    ]


def _ui_text_candidates(node: dict[str, Any]) -> list[str]:
    node_type = _node_type(node).lower()
    values: list[str] = []
    for key, value in _ui_widget_items(node):
        clean = _clean_text(value, str(key))
        if not clean or _looks_like_model(clean):
            continue
        if any(
            hint in str(key).lower()
            for hint in ("strength", "seed", "sampler", "scheduler", "random", "toggle", "mode")
        ):
            continue
        if clean not in values:
            values.append(clean)
    # A text editor often stores the real prompt and a huge token JSON in
    # different widgets.  The JSON is filtered above; choose the longest
    # remaining human-readable prompt.
    if any(hint in node_type for hint in TEXT_ENCODER_HINTS + TEXT_NODE_HINTS):
        return [max(values, key=len)] if values else []
    return []


def _ui_direct_text(node: dict[str, Any]) -> list[str]:
    node_type = _node_type(node).lower()
    if any(hint in node_type for hint in TEXT_ENCODER_HINTS + TEXT_NODE_HINTS):
        candidates = _ui_text_candidates(node)
        if candidates:
            return candidates
    # Newer workflow snapshots may place the actual widget in inputs[].widget
    # even when the node type is not a recognized text encoder.
    for key, value in _ui_widget_items(node):
        if key.lower() in TEXT_KEYS and isinstance(value, str) and len(value.strip()) > 1:
            return [value.strip()]
    return []


def _ui_trace(
    node_id: str,
    nodes: dict[str, dict[str, Any]],
    links: dict[str, dict[str, Any]],
    visited: set[str],
    depth: int = 0,
) -> list[str]:
    if depth > 32 or len(visited) > 2000 or node_id in visited:
        return []
    visited.add(node_id)
    node = nodes.get(node_id)
    if not node:
        return []
    low_type = _node_type(node).lower()
    if not _ui_node_active(node):
        return []
    if "zeroout" in low_type:
        return []
    inputs = node.get("inputs")
    if isinstance(inputs, list):
        # A CLIPTextEncode may keep an old widget value while its text socket
        # is linked to a prompt editor; the linked value is authoritative.
        for item in inputs:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).lower()
            if name not in {"text", "prompt", "opt_text", "positive", "negative", "conditioning"}:
                continue
            link_id = item.get("link")
            if link_id is None:
                continue
            link = links.get(str(link_id))
            if not link:
                continue
            traced = _ui_trace(link["origin_id"], nodes, links, visited, depth + 1)
            if traced:
                return traced
    direct = _ui_direct_text(node)
    if direct:
        return direct
    out: list[str] = []
    if isinstance(inputs, list):
        for item in inputs:
            if not isinstance(item, dict):
                continue
            link_id = item.get("link")
            if link_id is None:
                continue
            link = links.get(str(link_id))
            if not link:
                continue
            if str(item.get("name", "")).lower() in SKIP_TRACE_KEYS:
                continue
            out.extend(_ui_trace(link["origin_id"], nodes, links, visited, depth + 1))
    return _dedupe(out)


def _ui_guider_prompts(
    guider_id: str,
    nodes: dict[str, dict[str, Any]],
) -> tuple[str | None, str | None]:
    node = nodes.get(guider_id)
    if not node:
        return None, None
    return _ui_input_link(node, "positive"), _ui_input_link(node, "negative")


def _ui_sampler_settings(node: dict[str, Any]) -> dict[str, Any]:
    node_type = _node_type(node)
    low = node_type.lower()
    values = node.get("widgets_values")
    result: dict[str, Any] = {
        "node_id": str(node.get("id", "")),
        "class_type": node_type,
    }
    if isinstance(values, dict):
        for key in ("seed", "noise_seed", "steps", "cfg", "sampler_name", "scheduler", "denoise"):
            if key in values:
                value = _num(values[key])
                if value is not None:
                    result["seed" if key == "noise_seed" else key] = value
        return result
    if not isinstance(values, list):
        return result
    if "ksampleradvanced" in low:
        mapping = {
            0: "seed",
            2: "steps",
            3: "cfg",
            4: "sampler_name",
            5: "scheduler",
            6: "start_at_step",
            7: "end_at_step",
        }
        for index, key in mapping.items():
            if index < len(values):
                value = _num(values[index])
                if value is not None:
                    result[key] = value
    elif "ksampler" in low:
        mapping = {0: "seed", 2: "steps", 3: "cfg", 4: "sampler_name", 5: "scheduler", 6: "denoise"}
        for index, key in mapping.items():
            if index < len(values):
                value = _num(values[index])
                if value is not None:
                    result[key] = value
    elif "samplercustom" in low:
        mapping = {0: "seed", 1: "cfg", 2: "sampler_name", 3: "scheduler", 4: "denoise"}
        for index, key in mapping.items():
            if index < len(values):
                value = _num(values[index])
                if value is not None:
                    result[key] = value
    return result


def _ui_dimensions(nodes: dict[str, dict[str, Any]]) -> tuple[int | None, int | None]:
    ordered = sorted(
        (node for node in nodes.values() if _ui_node_active(node)),
        key=lambda node: (
            0
            if any(hint in _node_type(node).lower() for hint in ("emptylatent", "latent", "empty"))
            else 1
        ),
    )
    for node in ordered:
        values = node.get("widgets_values")
        if isinstance(values, list) and len(values) >= 2:
            width, height = _num(values[0]), _num(values[1])
            if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
                return width, height
    return None, None


def _ui_models(nodes: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    result = {"checkpoints": [], "unets": [], "vaes": [], "clips": [], "controlnets": []}
    for node in nodes.values():
        if not _ui_node_active(node):
            continue
        low = _node_type(node).lower()
        strings = _ui_widget_strings(node)
        models = [value for value in strings if _looks_like_model(value)]
        if not models:
            continue
        if "checkpoint" in low:
            result["checkpoints"].extend(models[:1])
        elif "unet" in low:
            result["unets"].extend(models[:1])
        elif "vae" in low:
            result["vaes"].extend(models[:1])
        elif "clip" in low and "lora" not in low:
            result["clips"].extend(models[:1])
        elif "controlnet" in low:
            result["controlnets"].extend(models[:1])
    return {key: _dedupe(values) for key, values in result.items()}


def parse_ui_workflow(workflow: dict[str, Any]) -> dict[str, Any]:
    nodes = _ui_nodes(workflow)
    links = _ui_links(workflow)
    warnings: list[str] = []
    positive: list[str] = []
    negative: list[str] = []
    samplers: list[dict[str, Any]] = []

    sampler_nodes = []
    for node in nodes.values():
        low = _node_type(node).lower()
        if not _ui_node_active(node):
            continue
        if "select" in low:
            continue
        if "ksampler" in low or "samplercustom" in low:
            inputs = node.get("inputs")
            if not isinstance(inputs, list) or not any(
                isinstance(item, dict) and item.get("link") is not None for item in inputs
            ):
                continue
            if not any(
                _ui_input_link(node, name)
                for name in ("positive", "negative", "guider", "conditioning")
            ):
                continue
            sampler_nodes.append(node)

    for node in sampler_nodes:
        pos_link = _ui_input_link(node, "positive")
        neg_link = _ui_input_link(node, "negative")
        if "samplercustom" in _node_type(node).lower() and not pos_link and not neg_link:
            guider_link = _ui_input_link(node, "guider")
            if guider_link and guider_link in links:
                pos_link, neg_link = _ui_guider_prompts(links[guider_link]["origin_id"], nodes)
        if pos_link and pos_link in links:
            positive.extend(_ui_trace(links[pos_link]["origin_id"], nodes, links, set()))
        if neg_link and neg_link in links:
            negative.extend(_ui_trace(links[neg_link]["origin_id"], nodes, links, set()))
        samplers.append(_ui_sampler_settings(node))
        if not pos_link:
            warnings.append(f"工作流采样器 {node.get('id')} 未找到 positive 输入")

    if not sampler_nodes:
        for node in nodes.values():
            if not _ui_node_active(node):
                continue
            if any(hint in _node_type(node).lower() for hint in TEXT_ENCODER_HINTS):
                positive.extend(_ui_direct_text(node))

    used_ids: set[str] | None = None
    if sampler_nodes and links:
        used_ids = _ui_ancestor_ids(
            [str(node.get("id")) for node in sampler_nodes], nodes, links
        )
    def _has_wiring_ports(node: dict[str, Any]) -> bool:
        inputs = node.get("inputs")
        return isinstance(inputs, list) and any(
            isinstance(item, dict) and "link" in item for item in inputs
        )

    lora_nodes = [
        node
        for node in nodes.values()
        if used_ids is None
        or str(node.get("id")) in used_ids
        # Older widget-only workflow exports expose no link ports at all and
        # are still meaningful.  A node that has ports but none connected is
        # detached from the render and must not contribute LoRAs.
        or not _has_wiring_ports(node)
    ]
    record_names: set[str] = set()
    for node in lora_nodes:
        for _key, value in _ui_widget_items(node):
            record_names.update(_lora_record_names(value))
    tag_loras = _parse_lora_tags(_dedupe(positive + negative))
    if record_names:
        # ``<lora:...>`` text widgets often keep disabled entries as well.
        # Once a structured record list exists, it is the source of truth;
        # only keep prompt tags for LoRAs the manager did not know about.
        tag_loras = [
            item for item in tag_loras if _lora_name_key(item.get("name")) not in record_names
        ]
    loras = _merge_loras(
        [item for node in lora_nodes for item in _lora_from_workflow_widgets(node)],
        tag_loras,
    )
    models = _ui_models(nodes)
    width, height = _ui_dimensions(nodes)
    for sampler in samplers:
        if width and height:
            sampler.setdefault("width", width)
            sampler.setdefault("height", height)
    summary = [
        {
            "id": str(node.get("id")),
            "type": _node_type(node),
            "title": node.get("title", ""),
        }
        for node in nodes.values()
    ]
    return {
        "positive_prompt": _display_prompt("\n\n".join(_dedupe(positive))),
        "negative_prompt": _display_prompt("\n\n".join(_dedupe(negative))),
        "prompts": _dedupe(positive + negative),
        "loras": loras,
        "checkpoints": models["checkpoints"],
        "unets": models["unets"],
        "vaes": models["vaes"],
        "clips": models["clips"],
        "controlnets": models["controlnets"],
        "base_model": _base_model_name(models["checkpoints"], models["unets"]),
        "embeddings": [],
        "samplers": samplers,
        "width": width,
        "height": height,
        "node_count": len(nodes),
        "nodes": summary,
        "has_lora_records": bool(record_names),
        "warnings": warnings,
    }


def parse_comfy(
    prompt: dict[str, Any] | None,
    workflow: dict[str, Any] | None,
) -> dict[str, Any]:
    api = parse_api_prompt(prompt) if isinstance(prompt, dict) else None
    ui = parse_ui_workflow(workflow) if isinstance(workflow, dict) else None
    if not api and not ui:
        return {}
    if api and ui:
        positive = api["positive_prompt"] or ui["positive_prompt"]
        negative = api["negative_prompt"] or ui["negative_prompt"]
        # The API prompt is the graph ComfyUI actually executed; bypassed and
        # disconnected UI nodes are already absent from it.  LoraManager's UI
        # record list is more precise than prompt text because it carries an
        # explicit enabled flag, so prefer it when present.  Otherwise prefer
        # structured API loader nodes over text tags.
        if ui.get("has_lora_records"):
            loras = _merge_loras(ui["loras"])
        elif any(item.get("source") != "text" for item in api["loras"]):
            loras = _merge_loras(api["loras"])
        else:
            loras = _merge_loras(api["loras"] or ui["loras"])
        samplers = api["samplers"] or ui["samplers"]
        width = api["width"] or ui["width"]
        height = api["height"] or ui["height"]
        return {
            "kind": "comfyui",
            "positive_prompt": positive,
            "negative_prompt": negative,
            "prompts": _dedupe(api["prompts"] + ui["prompts"]),
            "loras": loras,
            "checkpoints": _dedupe(api["checkpoints"] + ui["checkpoints"]),
            "unets": _dedupe(api["unets"] + ui["unets"]),
            "vaes": _dedupe(api["vaes"] + ui["vaes"]),
            "clips": _dedupe(api["clips"] + ui["clips"]),
            "controlnets": _dedupe(api["controlnets"] + ui["controlnets"]),
            "base_model": (
                api.get("base_model")
                or ui.get("base_model")
                or _base_model_name(
                    _dedupe(api["checkpoints"] + ui["checkpoints"]),
                    _dedupe(api["unets"] + ui["unets"]),
                )
            ),
            "embeddings": _dedupe(api["embeddings"] + ui["embeddings"]),
            "samplers": samplers,
            "width": width,
            "height": height,
            "node_count": max(api["node_count"], ui["node_count"]),
            "nodes": api["nodes"] or ui["nodes"],
            "workflow_format": "api+ui",
            "warnings": _dedupe(api["warnings"] + ui["warnings"]),
        }
    chosen = api or ui
    assert chosen is not None
    chosen = dict(chosen)
    chosen.pop("has_lora_records", None)
    chosen["workflow_format"] = "api" if api else "ui"
    chosen.setdefault(
        "base_model",
        _base_model_name(chosen.get("checkpoints", []), chosen.get("unets", [])),
    )
    return chosen