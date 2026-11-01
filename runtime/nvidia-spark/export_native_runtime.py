"""Export NVIDIA's native ARM64 Dynamo bindings and their private shared libraries.

Run only in the pinned donor Docker stage. PyPI ARM64 wheels can omit the
multimodal/NIXL features compiled into NVIDIA's runtime image.
"""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import subprocess
import sys
from importlib.metadata import distribution
from pathlib import Path


def export(destination: Path) -> None:
    package = distribution("ai-dynamo-runtime")
    if package.version != "1.6.1" or platform.machine() != "aarch64":
        raise RuntimeError("Expected NVIDIA's ARM64 Dynamo 1.6.1 donor")
    root = Path(package.locate_file(""))
    for item in package.files or []:
        source = Path(package.locate_file(item))
        relative = source.relative_to(root)
        if ".." in relative.parts:
            raise ValueError(f"Unexpected runtime file outside site-packages: {relative}")
        target = destination / "site-packages" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    core = root / "dynamo/_core.abi3.so"
    dependencies = subprocess.check_output(["ldd", str(core)], text=True)
    if "not found" in dependencies:
        raise RuntimeError(dependencies)
    library_dir = destination / "lib"
    library_dir.mkdir(parents=True, exist_ok=True)
    for line in dependencies.splitlines():
        if "=> /usr/local/lib/" in line:
            source = Path(line.split("=>", 1)[1].split()[0])
            shutil.copy2(source, library_dir / source.name)
    (destination / "manifest.json").write_text(json.dumps({
        "version": package.version,
        "architecture": platform.machine(),
        "core_sha256": hashlib.sha256(core.read_bytes()).hexdigest(),
    }, indent=2) + "\n")


if __name__ == "__main__":
    export(Path(sys.argv[1]))
