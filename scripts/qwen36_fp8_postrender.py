#!/usr/bin/env python3
"""Adapt only the existing L40S frontend/worker to a preverified offline cache."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import yaml

from prepare_qwen36_fp8_cache import PIN_PATH, load_pin, manifest_digest

FRAMING_LAUNCHER = (
    Path(__file__).resolve().parents[1]
    / "runtime/qwen36-35b-fp8-l40s/frontend_tool_framing.py"
)
FRAMING_IMAGE = (
    "nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.6.0@sha256:"
    "c957d97ccb8b225d7671f39afc6b340d910f0c58f8d17837cce6078c77344cd0"
)


def startup_guard(pin: dict) -> str:
    return (
        "python3 -I - <<'PINNED_MODEL_CHECK'\n"
        + f"""from pathlib import Path
import hashlib, json, re
pin = {pin!r}
marker = json.loads(Path(pin['marker']).read_text())
assert marker['schema_version'] == 1
assert marker['model'] == pin['candidate_model']
assert marker['revision'] == pin['revision']
assert marker['pin_manifest_sha256'] == {manifest_digest(pin)!r}
assert marker['weights_lfs_sha256_verified'] is True
assert marker['tokenizer_and_metadata_verified'] is True
expected = {{item['file']: item['size'] for item in pin['weight_files']}}
assert set(marker['weight_sha256']) == set(expected)
assert all(re.fullmatch('[0-9a-f]{{64}}', h) for h in marker['weight_sha256'].values())
snapshot = Path(pin['snapshot'])
cache = snapshot.parents[2].resolve(strict=True)
def model_file(name):
    path = (snapshot / name).resolve(strict=True)
    assert path.is_relative_to(cache) and path.is_file()
    return path
for item in pin['supporting_files']:
    name, expected_sha256 = item['file'], item['sha256']
    assert marker['metadata_sha256'][name] == expected_sha256
    assert hashlib.sha256(model_file(name).read_bytes()).hexdigest() == expected_sha256
for name, size in expected.items():
    assert model_file(name).stat().st_size == size
PINNED_MODEL_CHECK
"""
    )


def adapt(objects: list[dict], pin: dict) -> list[dict]:
    objects = copy.deepcopy(objects)
    found = 0
    for obj in objects:
        if not obj or obj.get("kind") != "DynamoGraphDeployment":
            continue
        if obj["metadata"]["name"] != pin["old_name"]:
            raise ValueError("Unexpected graph deployment")
        found += 1
        spec = obj["spec"]
        if any(item["name"] == pin["new_cache"] for item in spec["pvcs"]):
            raise ValueError("Offline cache already patched")
        spec["pvcs"].append({"name": pin["new_cache"], "create": False})
        for component in ["Frontend", "VllmWorker"]:
            service = spec["services"][component]
            if service["volumeMounts"] != [
                {"name": pin["old_cache"], "mountPoint": "/model"}
            ]:
                raise ValueError("Unexpected model cache mount")
            service["volumeMounts"][0]["name"] = pin["new_cache"]
            container = service["extraPodSpec"]["mainContainer"]
            worker = component == "VllmWorker"
            if worker:
                container["env"] = [
                    item
                    for item in container["env"]
                    if not item["name"].startswith("MODEL_EXPRESS_")
                ]
            for item in container["env"]:
                if item["name"] in ["HF_HUB_CACHE", "MODEL_CACHE_PATH"]:
                    item["value"] = "/model/hf"
                if worker and item["name"] == "MODEL_NAME":
                    item["value"] = pin["candidate_model"]
            for name in [
                "HF_HUB_OFFLINE",
                "TRANSFORMERS_OFFLINE",
                "HF_HUB_DISABLE_IMPLICIT_TOKEN",
            ]:
                container["env"] = [
                    item for item in container["env"] if item["name"] != name
                ]
                container["env"].append({"name": name, "value": "1"})
            script = container["args"][0]
            entry = "exec python3 -m dynamo." + ("vllm" if worker else "frontend")
            execute = script.index(entry)
            if worker:
                wait = script.index('echo "Waiting for ModelExpress at ')
                script = script[:wait] + startup_guard(pin) + script[execute:]
                source = '--model "${MODEL_NAME}"'
                if script.count(source) != 1:
                    raise ValueError("Unexpected model command")
                script = script.replace(
                    source,
                    "--model "
                    + json.dumps(pin["snapshot"])
                    + " \\\n  --tokenizer "
                    + json.dumps(pin["snapshot"]),
                )
            else:
                script = script[:execute] + startup_guard(pin) + script[execute:]
            container["args"] = [script]
    if found != 1:
        raise ValueError("Expected one graph deployment")
    return objects


def adapt_frontend_framing(objects: list[dict], source: str) -> list[dict]:
    """Mount a version-guarded launcher without changing workers or services."""
    objects = copy.deepcopy(objects)
    digest = hashlib.sha256(source.encode()).hexdigest()
    name = f"qwen36-tool-framing-{digest[:16]}"
    mount = "/opt/dynamo-compat"
    filename = f"frontend_tool_framing_{digest[:16]}.py"
    found = 0
    for obj in objects:
        if not obj or obj.get("kind") != "DynamoGraphDeployment":
            continue
        if obj["metadata"]["name"] != "qwen38-27b-vllm-l40s":
            raise ValueError("Unexpected graph for frontend framing")
        found += 1
        frontend = obj["spec"]["services"]["Frontend"]
        pod = frontend["extraPodSpec"]
        container = pod["mainContainer"]
        if container["image"] != FRAMING_IMAGE:
            raise ValueError("Frontend framing requires the pinned runtime image")
        if pod.get("volumes") or container.get("volumeMounts"):
            raise ValueError("Unexpected frontend compatibility mounts")
        script = container["args"][0]
        entry = "exec python3 -m dynamo.frontend"
        if script.count(entry) != 1:
            raise ValueError("Unexpected frontend entrypoint")
        container["args"] = [script.replace(entry, f"exec python3 {mount}/{filename}")]
        pod["volumes"] = [{"name": "tool-framing", "configMap": {"name": name}}]
        container["volumeMounts"] = [
            {"name": "tool-framing", "mountPath": mount, "readOnly": True}
        ]
    if found != 1 or any(
        obj and obj.get("kind") == "ConfigMap" and obj["metadata"]["name"] == name
        for obj in objects
    ):
        raise ValueError("Expected one unpatched frontend")
    objects.append(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": name},
            "immutable": True,
            "data": {filename: source},
        }
    )
    return objects


def main() -> int:
    try:
        objects = list(yaml.safe_load_all(sys.stdin.read()))
        yaml.safe_dump_all(
            adapt_frontend_framing(
                adapt(objects, load_pin(PIN_PATH)), FRAMING_LAUNCHER.read_text()
            ),
            sys.stdout,
            sort_keys=False,
        )
    except Exception:
        print("Pinned offline model adaptation failed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
