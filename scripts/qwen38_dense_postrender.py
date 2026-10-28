#!/usr/bin/env python3
"""Stage the pinned dense checkpoint without changing shared serving resources."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys

import yaml

from qwen36_fp8_postrender import FRAMING_LAUNCHER, adapt_frontend_framing

ROOT = Path(__file__).resolve().parents[1]
PIN_PATH = ROOT / "runtime/qwen38-27b-l40s/model-pin.json"


def startup_guard(pin: dict) -> str:
    # Validate metadata and shard presence without rewriting the retained cache.
    # Shard sizes are a completeness check, not a claim of checksum verification.
    return "python3 -I - <<'DENSE_MODEL_CHECK'\n" + f"""from pathlib import Path
import hashlib, json
pin = {pin!r}
snapshot = Path(pin['snapshot'])
cache = snapshot.parents[2].resolve(strict=True)
def model_file(name):
    path = (snapshot / name).resolve(strict=True)
    assert path.is_relative_to(cache) and path.is_file()
    return path
for item in pin['supporting_files']:
    assert hashlib.sha256(model_file(item['file']).read_bytes()).hexdigest() == item['sha256']
index = json.loads(model_file('model.safetensors.index.json').read_text())
assert set(index['weight_map'].values()) == {{item['file'] for item in pin['weight_files']}}
for item in pin['weight_files']:
    assert model_file(item['file']).stat().st_size == item['size']
print('Pinned dense model metadata and shard completeness verified: ' + pin['revision'])
DENSE_MODEL_CHECK
"""


def adapt(objects: list[dict], pin: dict) -> list[dict]:
    objects = copy.deepcopy(objects)
    found = 0
    for obj in objects:
        if not obj or obj.get("kind") != "DynamoGraphDeployment":
            continue
        if obj["metadata"]["name"] != pin["graph"]:
            raise ValueError("Unexpected graph deployment")
        found += 1
        # Retain the previous MoE claim declaration for an explicit rollback.
        obj["spec"]["pvcs"].append(
            {"name": "qwen36-35b-fp8-l40s-model-cache", "create": False}
        )
        for component in ["Frontend", "VllmWorker"]:
            service = obj["spec"]["services"][component]
            if service["volumeMounts"] != [
                {"name": pin["cache"], "mountPoint": "/model"}
            ]:
                raise ValueError("Unexpected retained dense cache mount")
            container = service["extraPodSpec"]["mainContainer"]
            worker = component == "VllmWorker"
            if worker:
                container["env"] = [
                    item for item in container["env"]
                    if not item["name"].startswith("MODEL_EXPRESS_")
                ]
            for name in ["HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_HUB_DISABLE_IMPLICIT_TOKEN"]:
                container["env"] = [item for item in container["env"] if item["name"] != name]
                container["env"].append({"name": name, "value": "1"})
            if not worker:
                # Keep long prefills from losing registration to a short canary.
                container["env"].append(
                    {"name": "DYN_HEALTH_CHECK_REQUEST_TIMEOUT", "value": "120"}
                )
            script = container["args"][0]
            entry = "exec python3 -m dynamo." + ("vllm" if worker else "frontend")
            execute = script.index(entry)
            if worker:
                wait = script.index('echo "Waiting for ModelExpress at ')
                script = script[:wait] + startup_guard(pin) + script[execute:]
                source = '--model "${MODEL_NAME}"'
                if script.count(source) != 1:
                    raise ValueError("Unexpected worker model command")
                script = script.replace(
                    source,
                    "--model " + json.dumps(pin["snapshot"])
                    + " \\\n  --tokenizer " + json.dumps(pin["snapshot"]),
                )
            else:
                script = script[:execute] + startup_guard(pin) + script[execute:]
            container["args"] = [script]
    if found != 1:
        raise ValueError("Expected one graph deployment")
    return adapt_frontend_framing(objects, FRAMING_LAUNCHER.read_text())


def main() -> int:
    try:
        objects = list(yaml.safe_load_all(sys.stdin.read()))
        pin = json.loads(PIN_PATH.read_text())
        yaml.safe_dump_all(adapt(objects, pin), sys.stdout, sort_keys=False)
    except Exception:
        print("Pinned dense model adaptation failed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
