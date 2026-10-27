#!/usr/bin/env python3
"""Emit the opt-in Super cache PVC and CPU Job; never apply or download locally."""

from __future__ import annotations

import argparse
import ast
from pathlib import Path
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "runtime/nemotron35super-gb10/cache_integrity.py"
VALUES = ROOT / "values/staging/nemotron35super-gb10.yaml"


def render(namespace: str = "daedalus-dynamo") -> list[dict]:
    if namespace != "daedalus-dynamo":
        raise ValueError("This private stage supports only daedalus-dynamo")
    values = yaml.safe_load(VALUES.read_text())
    name = values["model"]["name"]
    cache = values["modelCache"]
    source = SOURCE.read_text()
    ast.parse(source)
    pvc = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": cache["name"],
            "namespace": namespace,
            "annotations": {
                "helm.sh/resource-policy": "keep",
                "meta.helm.sh/release-name": name,
                "meta.helm.sh/release-namespace": namespace,
            },
            "labels": {"app.kubernetes.io/managed-by": "Helm"},
        },
        "spec": {
            "accessModes": cache["accessModes"],
            "resources": {"requests": {"storage": cache["storage"]}},
            "storageClassName": cache["storageClassName"],
        },
    }
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name + "-cache", "namespace": namespace},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 14400,
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "nodeSelector": {
                        "kubernetes.io/hostname": values["worker"]["nodeSelector"][
                            "kubernetes.io/hostname"
                        ]
                    },
                    "tolerations": values["worker"]["tolerations"],
                    "securityContext": {
                        "runAsUser": 1000,
                        "runAsGroup": 1000,
                        "runAsNonRoot": True,
                        "fsGroup": 1000,
                        "fsGroupChangePolicy": "OnRootMismatch",
                    },
                    "containers": [
                        {
                            "name": "cache",
                            "image": values["model"]["image"],
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["python3", "-I", "-c", source],
                            "env": [
                                {"name": "HF_HOME", "value": "/model"},
                                {"name": "HF_HUB_DISABLE_IMPLICIT_TOKEN", "value": "1"},
                                {"name": "NVIDIA_VISIBLE_DEVICES", "value": "void"},
                                {"name": "CUDA_VISIBLE_DEVICES", "value": ""},
                            ],
                            "resources": {
                                "requests": {"cpu": "1", "memory": "1Gi"},
                                "limits": {"cpu": "2", "memory": "6Gi"},
                            },
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "volumeMounts": [{"name": "cache", "mountPath": "/model"}],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "cache",
                            "persistentVolumeClaim": {"claimName": cache["name"]},
                        }
                    ],
                }
            },
        },
    }
    return [pvc, job]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--namespace", choices=["daedalus-dynamo"], default="daedalus-dynamo"
    )
    args = parser.parse_args()
    yaml.safe_dump_all(render(args.namespace), sys.stdout, sort_keys=False)


if __name__ == "__main__":
    main()
