#!/usr/bin/env python3
"""Use the Mamba state block size when choosing hybrid prefill checkpoints."""

from __future__ import annotations

import ast
import hashlib
from importlib.metadata import version
from pathlib import Path


EXPECTED_VLLM = "0.32.0rc1.dev100+g77154ca61"
EXPECTED_SCHEDULER_SHA256 = "6168b723a3f4319db98ece117be3281c79a53b9c1b74e5309ddce4e463eaec47"
ORIGINAL = (
    "        block_size = self.cache_config.block_size\n"
    "        # The last block-aligned position whose state can be cached."
)
REPLACEMENT = (
    "        # A DFlash draft group can make cache_config.block_size smaller\n"
    "        # than the Mamba state blocks used by the checkpoint exporter.\n"
    "        block_size = self.cache_config.mamba_block_size\n"
    "        assert block_size is not None\n"
    "        # The last block-aligned position whose state can be cached."
)


def patch_source(source: str) -> str:
    if source.count(ORIGINAL) != 1:
        raise ValueError("expected exactly one original Mamba prefill block-size assignment")
    patched = source.replace(ORIGINAL, REPLACEMENT, 1)
    ast.parse(patched)
    return patched


def main() -> None:
    if version("vllm") != EXPECTED_VLLM:
        raise RuntimeError(f"this patch requires vLLM {EXPECTED_VLLM}")
    import vllm

    scheduler = Path(vllm.__file__).resolve().parent / "v1/core/sched/scheduler.py"
    source = scheduler.read_bytes()
    if hashlib.sha256(source).hexdigest() != EXPECTED_SCHEDULER_SHA256:
        raise RuntimeError("scheduler source differs from the audited pinned runtime; review before patching")
    scheduler.write_text(patch_source(source.decode()))
    print("Applied Mamba prefill checkpoint alignment correction")


if __name__ == "__main__":
    main()
