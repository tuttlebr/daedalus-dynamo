"""Check the pinned engine and Dynamo CLI contracts without loading weights."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import platform
import sys
from importlib.metadata import distribution, version
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FRONTEND_ONLY = {"tool-call-parser", "enable-auto-tool-choice", "default-chat-template-kwargs"}


def option_args(options: dict) -> list[str]:
    result = []
    for key, value in options.items():
        if value is None or value is False:
            continue
        result.append("--" + key)
        if value is not True:
            result.append(json.dumps(value) if isinstance(value, dict) else str(value))
    return result


def verify(profile: dict, expected: str, role: str) -> None:
    if version("vllm") != expected:
        raise RuntimeError(f"Expected vLLM {expected}, found {version('vllm')}")
    for name in ("ai-dynamo", "ai-dynamo-runtime"):
        if version(name) != "1.6.1":
            raise RuntimeError(f"Unexpected {name} version")
    if platform.machine() != "aarch64":
        raise RuntimeError("These recipes require an ARM64 runtime")
    native = json.loads(Path("/opt/dynamo-native/manifest.json").read_text())
    core = Path(distribution("ai-dynamo-runtime").locate_file("dynamo/_core.abi3.so"))
    if hashlib.sha256(core.read_bytes()).hexdigest() != native["core_sha256"]:
        raise RuntimeError("NVIDIA's native binding was replaced")
    importlib.import_module("dynamo._core")
    options = profile["options"]
    if "kernel-config" in options:
        from vllm.config.kernel import KernelConfig

        KernelConfig(**options["kernel-config"])
    if role in ("worker", "all"):
        importlib.import_module("dynamo.vllm.main")
        # This version-pinned parser does not fetch model metadata or create an
        # engine. It catches unsupported flags/loaders before GPU qualification.
        from dynamo.vllm.args import _parse_cli_args

        worker = {k: v for k, v in options.items() if k not in FRONTEND_ONLY | {"host", "port"}}
        _parse_cli_args(["--model", profile["model"], "--frontend-decoding", *option_args(worker)])
    if role in ("frontend", "all"):
        from dynamo.frontend.main import parse_args

        frontend = {k: v for k, v in options.items() if k in FRONTEND_ONLY | {"reasoning-parser"}}
        previous = sys.argv
        try:
            sys.argv = ["dynamo.frontend", "--dyn-chat-processor", "vllm", *option_args(frontend)]
            parse_args()
        finally:
            sys.argv = previous
    print(f"Runtime contract passed: {profile['model']} ({role})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--runtime")
    selection.add_argument("--profile")
    parser.add_argument("--role", choices=("worker", "frontend", "all"), default="all")
    args = parser.parse_args()
    lock = json.loads((ROOT / "nvidia-spark.json").read_text())
    profiles = [p for p in lock["profiles"]
                if p["runtime"] == args.runtime or p["model"] == args.profile]
    if not profiles:
        parser.error("Unknown recipe/runtime")
    for profile in profiles:
        verify(profile, lock["runtimes"][profile["runtime"]]["expected_vllm"], args.role)


if __name__ == "__main__":
    main()
