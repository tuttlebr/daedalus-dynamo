#!/usr/bin/env python3
"""Render-only adapter; never invokes Kubernetes or downloads a model."""

import copy
import json
import sys
from pathlib import Path

import yaml

REVISION = "da5b2650a835292c678994a1fa3fb653250fb508"
NAME = "nemotron35super-gb10-stage"
INTEGRITY_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "runtime/nemotron35super-gb10/cache_integrity.py"
)
SNAPSHOT = (
    "/model/hf/models--nvidia--NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4/snapshots/"
    + REVISION
)


def adapt(objects):
    objects = copy.deepcopy(objects)
    ds = [o for o in objects if o and o["kind"] == "DynamoGraphDeployment"]
    assert len(ds) == 1
    d = ds[0]
    assert d["metadata"]["name"] == NAME
    services = d["spec"]["services"]
    assert set(services) == {"Frontend", "VllmWorker"}
    integrity = INTEGRITY_SOURCE.read_text().split("# DOWNLOAD_ENTRYPOINT", 1)[0]
    check = (
        "python3 -I - <<'CHECK'\n"
        + integrity
        + "\nvalidate_offline(Path("
        + repr(SNAPSHOT)
        + "), Path('/model/hf'))\nCHECK\n"
    )
    for key, service in services.items():
        assert service.pop("envFromSecret") == "stage-placeholder-must-be-removed"
        container = service["extraPodSpec"]["mainContainer"]
        assert not any(k in container for k in ["envFrom", "secretKeyRef"])
        env = container["env"]
        env[:] = [e for e in env if not e["name"].startswith("MODEL_EXPRESS_")]
        env.extend(
            [
                {"name": "HF_HUB_OFFLINE", "value": "1"},
                {"name": "TRANSFORMERS_OFFLINE", "value": "1"},
                {"name": "HF_HUB_DISABLE_IMPLICIT_TOKEN", "value": "1"},
            ]
        )
        script = container["args"][0]
        if key == "VllmWorker":
            marker = 'echo "Waiting for ModelExpress at '
            start = script.index(marker)
            execute = script.index("exec python3 -m dynamo.vllm")
            assert start < execute
            script = script[:start] + check + script[execute:]
            original = '--model "${MODEL_NAME}"'
            assert script.count(original) == 1
            script = script.replace(
                original,
                "--model "
                + json.dumps(SNAPSHOT)
                + " \\\n  --tokenizer "
                + json.dumps(SNAPSHOT),
            )
        else:
            execute = script.index("exec python3 -m dynamo.frontend")
            script = script[:execute] + check + script[execute:]
        container["args"] = [script]
    objects.append(
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": NAME + "-private",
                "namespace": d["metadata"].get("namespace", "daedalus-dynamo"),
                "labels": {"app.kubernetes.io/instance": NAME},
            },
            "spec": {
                "type": "ClusterIP",
                "selector": {
                    "nvidia.com/dynamo-component": "Frontend",
                    "nvidia.com/dynamo-component-type": "frontend",
                    "nvidia.com/dynamo-namespace": d["metadata"].get(
                        "namespace", "daedalus-dynamo"
                    )
                    + "-"
                    + NAME,
                },
                "ports": [{"name": "http", "port": 8000, "targetPort": "http"}],
            },
        }
    )
    return objects


if __name__ == "__main__":
    source = list(yaml.safe_load_all(sys.stdin.read()))
    result = adapt(source)
    yaml.safe_dump_all(result, sys.stdout, sort_keys=False)
