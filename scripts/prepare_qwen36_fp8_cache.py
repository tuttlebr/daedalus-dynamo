#!/usr/bin/env python3
"""Render a CPU-only preparation Job or prepare its exact-revision model cache."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PIN_PATH = ROOT / "runtime/qwen36-35b-fp8-l40s/model-pin.json"
MODEL = "Qwen/Qwen3.6-35B-A3B-FP8"
REVISION = "95a723d08a9490559dae23d0cff1d9466213d989"
IMAGE = (
    "nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.4.1@sha256:"
    "a6bb45ad652f01d08b5a18b54b9fa9e80584ce18ee1ba1e5a760a8f228872fe5"
)


def manifest_digest(pin: dict) -> str:
    return hashlib.sha256(
        json.dumps(pin, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def load_pin(path: Path) -> dict:
    pin = json.loads(path.read_text())
    critical = {
        "candidate_model": MODEL,
        "revision": REVISION,
        "old_name": "qwen38-27b-vllm-l40s",
        "old_model": "Qwen/Qwen3.8-27B-FP8",
        "old_cache": "qwen38-27b-l40s-model-cache",
        "new_cache": "qwen36-35b-fp8-l40s-model-cache",
        "snapshot": f"/model/hf/models--Qwen--Qwen3.6-35B-A3B-FP8/snapshots/{REVISION}",
        "marker": "/model/fp8-95a723d0-verified.json",
    }
    if any(pin.get(key) != value for key, value in critical.items()):
        raise ValueError("Unexpected model/cache pin")
    if len(pin["weight_files"]) != 42:
        raise ValueError("Unexpected model shard count")
    if {item["file"] for item in pin["supporting_files"]} != {
        "config.json",
        "model.safetensors.index.json",
        "generation_config.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "chat_template.jinja",
    } or any(
        not re.fullmatch(r"[0-9a-f]{64}", h)
        for h in [item["sha256"] for item in pin["supporting_files"]]
    ):
        raise ValueError("Incomplete model/tokenizer pin")
    return pin


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def field(value, name):
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def verify_snapshot(pin: dict, cache: Path, metadata) -> dict:
    """Check every indexed shard against exact-revision Hub LFS metadata."""
    if field(metadata, "sha") != pin["revision"]:
        raise ValueError("Hub revision does not match the pin")
    repo = "models--" + pin["candidate_model"].replace("/", "--")
    snapshot = cache / repo / "snapshots" / pin["revision"]

    def local_file(name: str) -> Path:
        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise ValueError("Unexpected model filename")
        resolved = (snapshot / name).resolve(strict=True)
        if not resolved.is_relative_to(cache.resolve()) or not resolved.is_file():
            raise ValueError("Model file escapes the cache")
        return resolved

    for name, key in [
        ("config.json", "config_sha256"),
        ("model.safetensors.index.json", "index_sha256"),
    ]:
        if file_digest(local_file(name)) != pin[key]:
            raise ValueError("Model metadata checksum mismatch")
    for item in pin["supporting_files"]:
        name, expected_sha256 = item["file"], item["sha256"]
        if file_digest(local_file(name)) != expected_sha256:
            raise ValueError("Pinned supporting-file checksum mismatch")
    expected = {item["file"]: item["size"] for item in pin["weight_files"]}
    index = json.loads(local_file("model.safetensors.index.json").read_text())
    if len(expected) != 42 or set(index["weight_map"].values()) != set(expected):
        raise ValueError("Model shard index does not match the pin")
    siblings = {
        field(item, "rfilename"): item for item in field(metadata, "siblings") or []
    }
    checksums = {}
    for name, size in expected.items():
        item = siblings.get(name)
        checksum = field(field(item, "lfs"), "sha256")
        if (
            field(item, "size") != size
            or not isinstance(checksum, str)
            or not re.fullmatch(r"[0-9a-f]{64}", checksum)
        ):
            raise ValueError("Pinned Hub LFS metadata is incomplete")
        path = local_file(name)
        if path.stat().st_size != size or file_digest(path) != checksum:
            raise ValueError("Model shard checksum mismatch")
        checksums[name] = checksum
    required = {"tokenizer.json", "tokenizer_config.json", "chat_template.jinja"}
    metadata_names = set(siblings) - set(expected)
    if not required <= metadata_names:
        raise ValueError("Tokenizer metadata is incomplete")
    metadata_sha256 = {}
    for name in sorted(metadata_names):
        item = siblings[name]
        path = local_file(name)
        if path.stat().st_size != field(item, "size"):
            raise ValueError("Model metadata size mismatch")
        sha256 = file_digest(path)
        lfs_sha = field(field(item, "lfs"), "sha256")
        if lfs_sha:
            matches = sha256 == lfs_sha
        else:
            # Hub metadata identifies ordinary files by their Git blob ID.
            digest = hashlib.sha1(
                f"blob {path.stat().st_size}\0".encode(), usedforsecurity=False
            )
            with path.open("rb") as stream:
                while block := stream.read(8 * 1024 * 1024):
                    digest.update(block)
            matches = digest.hexdigest() == field(item, "blob_id")
        if not matches:
            raise ValueError("Model/tokenizer metadata checksum mismatch")
        metadata_sha256[name] = sha256
    return {
        "schema_version": 1,
        "model": pin["candidate_model"],
        "revision": pin["revision"],
        "pin_manifest_sha256": manifest_digest(pin),
        "weights_lfs_sha256_verified": True,
        "weight_sha256": checksums,
        "metadata_sha256": metadata_sha256,
        "tokenizer_and_metadata_verified": True,
    }


def write_marker(path: Path, marker: dict) -> None:
    """Publish proof only after complete verification, with an atomic replace."""
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary = Path(name)
    published = False
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(marker, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        published = True
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        if published:
            path.unlink(missing_ok=True)
        raise
    finally:
        temporary.unlink(missing_ok=True)


def prepare(pin: dict, cache: Path = Path("/model/hf")) -> None:
    marker = Path(pin["marker"])
    # Never leave an earlier success marker visible after a failed new check.
    marker.unlink(missing_ok=True)
    from huggingface_hub import HfApi

    print(json.dumps({"phase": "download", "revision": pin["revision"]}), flush=True)
    subprocess.run(
        [
            "hf",
            "download",
            pin["candidate_model"],
            "--revision",
            pin["revision"],
            "--cache-dir",
            str(cache),
            "--max-workers",
            "2",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    metadata = HfApi(endpoint="https://huggingface.co").model_info(
        pin["candidate_model"], revision=pin["revision"], files_metadata=True
    )
    write_marker(marker, verify_snapshot(pin, cache, metadata))
    print(json.dumps({"verified": True, "revision": pin["revision"], "shards": 42}))


def render_job(pin: dict, job_name: str, namespace: str) -> list[dict]:
    if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,47}[a-z0-9])?", job_name):
        raise ValueError("Use a short Kubernetes job name")
    tools = job_name + "-tools"
    config = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": tools, "namespace": namespace},
        "data": {
            "prepare_cache.py": Path(__file__).read_text(),
            "model-pin.json": json.dumps(pin, sort_keys=True),
        },
    }
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "namespace": namespace,
            "annotations": {"daedalus.local/model-pin-sha256": manifest_digest(pin)},
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 7200,
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "nodeSelector": {"kubernetes.io/hostname": "daedalus-03"},
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 1000,
                        "runAsGroup": 1000,
                        "fsGroup": 1000,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "cache",
                            "image": IMAGE,
                            "imagePullPolicy": "IfNotPresent",
                            "command": [
                                "python3",
                                "/tools/prepare_cache.py",
                                "prepare",
                                "--pin",
                                "/tools/model-pin.json",
                            ],
                            "resources": {
                                "requests": {"cpu": "1", "memory": "4Gi"},
                                "limits": {"cpu": "2", "memory": "8Gi"},
                            },
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "env": [
                                {
                                    "name": "HF_TOKEN",
                                    "valueFrom": {
                                        "secretKeyRef": {
                                            "name": "hf-token-secret",
                                            "key": "HF_TOKEN",
                                            "optional": True,
                                        }
                                    },
                                },
                                *[
                                    {"name": name, "value": value}
                                    for name, value in [
                                        ("HF_ENDPOINT", "https://huggingface.co"),
                                        ("HF_HUB_OFFLINE", "0"),
                                        ("TRANSFORMERS_OFFLINE", "0"),
                                        ("HF_HUB_CACHE", "/model/hf"),
                                        ("HF_HOME", "/tmp/hf-home"),
                                        ("HF_HUB_DISABLE_XET", "1"),
                                        ("HF_HUB_DISABLE_TELEMETRY", "1"),
                                        ("HF_HUB_DISABLE_PROGRESS_BARS", "1"),
                                        ("NVIDIA_VISIBLE_DEVICES", "void"),
                                        ("CUDA_VISIBLE_DEVICES", ""),
                                    ]
                                ],
                            ],
                            "volumeMounts": [
                                {"name": "cache", "mountPath": "/model"},
                                {
                                    "name": "tools",
                                    "mountPath": "/tools",
                                    "readOnly": True,
                                },
                                {"name": "temporary", "mountPath": "/tmp"},
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "cache",
                            "persistentVolumeClaim": {"claimName": pin["new_cache"]},
                        },
                        {"name": "tools", "configMap": {"name": tools}},
                        {"name": "temporary", "emptyDir": {"sizeLimit": "2Gi"}},
                    ],
                }
            },
        },
    }
    return [config, job]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["render-job", "prepare"])
    parser.add_argument("--pin", type=Path, default=PIN_PATH)
    parser.add_argument("--job-name")
    parser.add_argument("--namespace", default="daedalus-dynamo")
    args = parser.parse_args()
    try:
        pin = load_pin(args.pin)
        if args.operation == "render-job":
            if not args.job_name:
                raise ValueError("A unique job name is required")
            yaml.safe_dump_all(
                render_job(pin, args.job_name, args.namespace),
                sys.stdout,
                sort_keys=False,
            )
        else:
            prepare(pin)
    except Exception:
        print(
            "Pinned model cache preparation failed; no success marker was published.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
