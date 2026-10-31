#!/usr/bin/env python3
"""Resolve reusable Helm values layers. Mappings merge; lists replace in full."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def merge(base: dict, overlay: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def resolve(path: Path, parents: tuple[Path, ...] = ()) -> dict:
    path = path.resolve()
    if path in parents:
        raise ValueError("values inheritance cycle: " + " -> ".join(map(str, (*parents, path))))
    document = yaml.safe_load(path.read_text())
    if not isinstance(document, dict):
        raise ValueError(f"{path}: expected a values mapping")
    includes = document.pop("extends", [])
    if not isinstance(includes, list) or any(not isinstance(p, str) for p in includes):
        raise ValueError(f"{path}: extends must be a list of relative file paths")
    result: dict = {}
    for include in includes:
        if Path(include).is_absolute():
            raise ValueError(f"{path}: extends paths must be relative")
        result = merge(result, resolve(path.parent / include, (*parents, path)))
    return merge(result, document)


def helm_values(document: dict) -> dict:
    return {key: value for key, value in document.items() if key != "recipe"}


def values_path(path: Path) -> str:
    # Flat legacy files stay byte-for-byte intact and retain their CLI path.
    raw = yaml.safe_load(path.read_text())
    if isinstance(raw, dict) and "extends" not in raw and "recipe" not in raw:
        return str(path)
    content = yaml.safe_dump(helm_values(resolve(path)), sort_keys=False)
    digest = hashlib.sha256(content.encode()).hexdigest()
    output = ROOT / "tmp" / "resolved-values" / f"{digest}.yaml"
    output.parent.mkdir(parents=True, exist_ok=True)
    if not output.exists():
        # Content-addressed files permit concurrent renders of different recipes.
        import os
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", dir=output.parent, delete=False) as stream:
            stream.write(content)
            temporary = stream.name
        os.replace(temporary, output)
    return str(output)


def list_recipes(directory: Path) -> list[Path]:
    paths = []
    for path in sorted(directory.rglob("*.yaml")):
        if any(part.startswith("_") for part in path.relative_to(directory).parts):
            continue
        raw = yaml.safe_load(path.read_text())
        if isinstance(raw, dict) and ("recipe" in raw or "id" in raw.get("model", {})):
            if path.name != "model.yaml":
                paths.append(path)
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("resolve", "path", "get", "list"))
    parser.add_argument("path", type=Path)
    parser.add_argument("key", nargs="?")
    args = parser.parse_args()
    try:
        if args.command == "list":
            print("\n".join(map(str, list_recipes(args.path))))
        elif args.command == "path":
            print(values_path(args.path))
        else:
            data = resolve(args.path)
            if args.command == "resolve":
                print(yaml.safe_dump(helm_values(data), sort_keys=False), end="")
            else:
                if not args.key:
                    parser.error("get requires a dotted key")
                value = data
                for key in args.key.split("."):
                    value = value.get(key, "") if isinstance(value, dict) else ""
                print(json.dumps(value) if isinstance(value, (dict, list, bool)) else value)
    except (OSError, ValueError, yaml.YAMLError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
