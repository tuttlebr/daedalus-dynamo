#!/usr/bin/env python3
"""Preserve Muse's tool channel across Dynamo's streaming reasoning handoff."""

from __future__ import annotations

import ast
import hashlib
from importlib.metadata import distribution, version
from pathlib import Path


EXPECTED_SOURCE_SHA256 = "2411b3543a19481f993fa09abb90ef13553b09d418a45263b60093e981d6c2e3"
ORIGINAL = "class MuseGlimmerToolParser(ToolParser):\n"
REPLACEMENT = ORIGINAL + '''    # Dynamo discovers this attribute at the reasoning-to-tool transition.
    # Without it, the handoff chunk is emitted as text and lost to the parser.
    # Buffer until finish_reason: an end marker would flush early and let the
    # Dynamo 1.6.1 adapter replay stale text, duplicating single-token calls.
    tool_call_start_token = "<atem:function_calls>"

'''


def patch_source(source: str) -> str:
    if hashlib.sha256(source.encode()).hexdigest() != EXPECTED_SOURCE_SHA256:
        raise ValueError("Muse parser differs from the audited runtime; review before patching")
    if source.count(ORIGINAL) != 1:
        raise ValueError("expected exactly one MuseGlimmerToolParser declaration")
    patched = source.replace(ORIGINAL, REPLACEMENT, 1)
    ast.parse(patched)
    return patched


def main() -> None:
    if version("vllm") != "0.31.0" or version("ai-dynamo") != "1.6.1":
        raise RuntimeError("this correction requires vLLM 0.31.0 and Dynamo 1.6.1")
    parser = Path(distribution("vllm").locate_file("vllm/tool_parsers/muse_glimmer_tool_parser.py"))
    parser.write_text(patch_source(parser.read_text()))
    print("Applied Muse Glimmer streaming tool handoff correction")


if __name__ == "__main__":
    main()
