"""Add Dynamo without replacing the selected NVIDIA recipe's engine stack."""

from __future__ import annotations

import ast
import hashlib
import importlib
import json
import shutil
import subprocess
import sys
from importlib.metadata import distribution, distributions, version
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EMBEDDING_SHA256 = "21e953a23a3b27623361ade3fcc224d93b194599436206980c9d77bff7db260f"


def defer_embedding_import(source: str) -> str:
    """Adapt the unused embedding process API, not generation semantics.

    Newer vLLM removed this helper. Dynamo 1.6.1 imports it eagerly even for
    generation workers. Keep it inside its sole embedding-only caller.
    """
    if hashlib.sha256(source.encode()).hexdigest() != EMBEDDING_SHA256:
        raise ValueError("Unexpected Dynamo embedding helper; revalidate the adapter")
    function = next(n for n in ast.parse(source).body
                    if isinstance(n, ast.FunctionDef) and n.name == "create_shared_embedding_engine_client")
    lines = source.splitlines(keepends=True)
    lines.insert(function.body[0].end_lineno,
                 "    from vllm.v1.engine.utils import get_engine_zmq_addresses\n")
    patched = "".join(lines).replace(
        "from vllm.v1.engine.utils import get_engine_zmq_addresses, launch_core_engines",
        "from vllm.v1.engine.utils import launch_core_engines", 1,
    )
    ast.parse(patched)
    return patched


def main() -> None:
    protected = {
        d.metadata["Name"]: d.version for d in distributions()
        if d.metadata["Name"].lower().replace("_", "-").startswith(
            ("vllm", "torch", "triton", "flashinfer", "transformers", "numpy", "nvidia-", "cuda-"))
    }
    constraints = ROOT / "engine-constraints.txt"
    constraints.write_text("".join(f"{name}=={v}\n" for name, v in sorted(protected.items())))
    subprocess.run([
        sys.executable, "-m", "pip", "install", "--no-cache-dir", "-c", str(constraints),
        "ai-dynamo==1.6.1", "modelexpress==0.7.0", "nixl[cu13]==1.4.1",
        "blake3==1.0.8", "uvloop==0.22.1", "protobuf==6.33.6",
        "grpcio-tools==1.76.0", "grpcio==1.84.0",
    ], check=True)
    for name, expected in protected.items():
        if version(name) != expected:
            raise RuntimeError(f"Engine dependency changed: {name}")

    native = Path("/opt/dynamo-native")
    site = Path(distribution("ai-dynamo-runtime").locate_file(""))
    shutil.copytree(native / "site-packages", site, dirs_exist_ok=True)
    native_manifest = json.loads((native / "manifest.json").read_text())
    if hashlib.sha256((site / "dynamo/_core.abi3.so").read_bytes()).hexdigest() != native_manifest["core_sha256"]:
        raise RuntimeError("Native Dynamo binding copy failed")

    utilities = importlib.import_module("vllm.v1.engine.utils")
    if not hasattr(utilities, "get_engine_zmq_addresses"):
        embedding = Path(distribution("ai-dynamo").locate_file("dynamo/vllm/embedding_worker_processes.py"))
        embedding.write_text(defer_embedding_import(embedding.read_text()))
    print("Installed Dynamo 1.6.1 with NVIDIA's native bindings; engine versions preserved")


if __name__ == "__main__":
    main()
