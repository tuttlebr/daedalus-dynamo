"""Narrow, version-pinned frontend-only Qwen empty-argument framing adapter."""

from __future__ import annotations

from copy import deepcopy
import functools
import hashlib
import importlib.metadata
import inspect
import json
from pathlib import Path
import re
import runpy
import sys
from typing import Any

_EXPECTED_VERSION = "0.2.3"
_EXPECTED_BUILDER_SHA256 = (
    "4543784a700f855dd8118297695eda375f169c75215399f3b662dbe985ba99b6"
)
_MODELS = ("qwen_3_coder", "qwen_3_5")
_LEGACY_END = "\n</function>\n</tool_call>"
_CANONICAL_END = "</function>\n</tool_call>"
_BEGIN = re.compile(r"<tool_call>\n<function=[A-Za-z0-9_-]{1,64}>\n\Z")
_PATCH_RESULT: dict[str, Any] | None = None


def _exact_empty_object(schema: Any) -> bool:
    if not isinstance(schema, dict):
        return False
    allowed = {
        "type",
        "properties",
        "additionalProperties",
        "required",
        "title",
        "description",
    }
    if set(schema) - allowed:
        return False
    if schema.get("type") != "object" or not isinstance(schema.get("properties"), dict):
        return False
    if schema["properties"] or schema.get("additionalProperties") is not False:
        return False
    if "required" in schema and schema["required"] != []:
        return False
    if any(
        key in schema and not isinstance(schema[key], str)
        for key in ("title", "description")
    ):
        return False
    return True


def normalize_qwen_tag(tag: dict[str, Any]) -> dict[str, Any]:
    """Copy a known Qwen builder result, changing only exact empty-object ends."""
    result = deepcopy(tag)

    def visit(node: Any) -> None:
        if not isinstance(node, dict):
            return
        kind = node.get("type")
        content = node.get("content")
        if (
            kind == "tag"
            and isinstance(node.get("begin"), str)
            and _BEGIN.fullmatch(node["begin"])
            and node.get("end") == _LEGACY_END
            and isinstance(content, dict)
            and content.get("type") == "json_schema"
            and content.get("style") == "qwen_xml"
            and _exact_empty_object(content.get("json_schema"))
        ):
            node["end"] = [_LEGACY_END, _CANONICAL_END]
            return
        # Never descend into JSON schema data or unknown format kinds.
        children = {
            "structural_tag": ("format",),
            "sequence": ("elements",),
            "or": ("elements",),
            "tag": ("content",),
            "tags_with_separator": ("tags",),
            "triggered_tags": ("tags",),
        }.get(kind, ())
        for key in children:
            child = node.get(key)
            if isinstance(child, list):
                for item in child:
                    visit(item)
            else:
                visit(child)

    visit(result)
    return result


def apply_patch() -> dict[str, Any]:
    """Register a process-local adapter; never modify installed files."""
    global _PATCH_RESULT
    import xgrammar
    from xgrammar import builtin_structural_tag as builtin

    version = importlib.metadata.version("xgrammar")
    source = Path(inspect.getsourcefile(builtin) or "")
    actual_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    if version != _EXPECTED_VERSION or actual_hash != _EXPECTED_BUILDER_SHA256:
        raise RuntimeError(
            "Qwen framing adapter rejected an unrecognized XGrammar build"
        )
    if _PATCH_RESULT is not None:
        return {**_PATCH_RESULT, "already_applied": True}
    original = builtin.get_qwen_3_5_structural_tag

    @functools.wraps(original)
    def corrected_builder(*args: Any, **kwargs: Any) -> Any:
        tag = original(*args, **kwargs)
        original_value = tag.model_dump(mode="json")
        normalized = normalize_qwen_tag(original_value)
        if normalized == original_value:
            return tag
        return xgrammar.StructuralTag.model_validate(normalized)

    for model in _MODELS:
        xgrammar.register_model_structural_tag(model)(corrected_builder)
    _PATCH_RESULT = {
        "xgrammar_version": version,
        "builder_source_sha256": actual_hash,
        "registered_models": list(_MODELS),
        "installed_files_modified": False,
        "already_applied": False,
    }
    return dict(_PATCH_RESULT)


def main() -> None:
    receipt = apply_patch()
    print(
        "DAEDALUS_QWEN_EMPTY_ARGS_ADAPTER=" + json.dumps(receipt, sort_keys=True),
        file=sys.stderr,
        flush=True,
    )
    sys.argv[0] = "dynamo.frontend"
    runpy.run_module("dynamo.frontend", run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
