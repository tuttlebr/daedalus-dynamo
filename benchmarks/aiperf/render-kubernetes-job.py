#!/usr/bin/env python3
"""Render the bounded all-model AIPerf Kubernetes Job as JSON."""

from __future__ import annotations

import json
import os
import re
import sys
from typing import NoReturn
from urllib.parse import urlsplit


DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
SECRET_KEY_RE = re.compile(r"^[A-Za-z0-9._-]+$")
QUANTITY_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?(?:m|Ki|Mi|Gi|Ti|Pi|Ei)?$")


def fail(message: str) -> NoReturn:
    raise SystemExit(f"ERROR: {message}")


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        fail(f"{name} must not be empty")
    if "\n" in value or "\r" in value:
        fail(f"{name} must not contain line breaks")
    return value


def optional_env(name: str, default: str = "") -> str:
    value = os.environ.get(name, default)
    if "\n" in value or "\r" in value:
        fail(f"{name} must not contain line breaks")
    return value


def positive_int(name: str, default: str) -> int:
    value = env(name, default)
    try:
        number = int(value)
    except ValueError:
        fail(f"{name} must be a positive integer (got {value})")
    if number <= 0:
        fail(f"{name} must be a positive integer (got {value})")
    return number


def boolean(name: str, default: str) -> str:
    value = env(name, default)
    if value not in {"0", "1"}:
        fail(f"{name} must be 0 or 1 (got {value})")
    return value


def dns_label(name: str, value: str) -> str:
    if len(value) > 63 or not DNS_LABEL_RE.fullmatch(value):
        fail(f"{name} must be a valid DNS label (got {value})")
    return value


def quantity(name: str, default: str) -> str:
    value = env(name, default)
    if not QUANTITY_RE.fullmatch(value):
        fail(f"{name} must be a simple Kubernetes resource quantity (got {value})")
    return value


def job_name(run_id: str) -> str:
    suffix = re.sub(r"[^a-z0-9-]+", "-", run_id.lower()).strip("-")
    if not suffix:
        fail("AIPERF_RUN_ID does not contain characters usable in a Job name")
    suffix = suffix[:42].rstrip("-")
    return f"aiperf-agentic-{suffix}"


def env_var(name: str, value: str) -> dict[str, str]:
    return {"name": name, "value": value}


def main() -> None:
    namespace = dns_label("NAMESPACE", env("NAMESPACE", "daedalus-dynamo"))
    image = env("AIPERF_IMAGE")
    if any(char.isspace() for char in image):
        fail("AIPERF_IMAGE must not contain whitespace")

    run_id = env("AIPERF_RUN_ID")
    if (
        run_id in {".", ".."}
        or len(run_id) > 128
        or not RUN_ID_RE.fullmatch(run_id)
    ):
        fail(
            "AIPERF_RUN_ID may contain at most 128 letters, numbers, dots, "
            "underscores, and hyphens"
        )

    endpoint_url = env("AIPERF_ENDPOINT_URL")
    endpoint = urlsplit(endpoint_url)
    try:
        _ = endpoint.port
    except ValueError:
        fail("AIPERF_ENDPOINT_URL contains an invalid port")
    if (
        endpoint.scheme not in {"http", "https"}
        or not endpoint.hostname
        or endpoint.username
        or endpoint.password
        or endpoint.query
        or endpoint.fragment
    ):
        fail(
            "AIPERF_ENDPOINT_URL must be an http(s) base URL without "
            "credentials, query, or fragment"
        )

    pull_policy = env("AIPERF_IMAGE_PULL_POLICY", "Always")
    if pull_policy not in {"Always", "IfNotPresent", "Never"}:
        fail("AIPERF_IMAGE_PULL_POLICY must be Always, IfNotPresent, or Never")

    api_key_secret = optional_env("AIPERF_API_KEY_SECRET")
    api_key_secret_key = optional_env("AIPERF_API_KEY_SECRET_KEY", "OPENAI_API_KEY")
    if api_key_secret:
        dns_label("AIPERF_API_KEY_SECRET", api_key_secret)
        if (
            not api_key_secret_key
            or len(api_key_secret_key) > 253
            or not SECRET_KEY_RE.fullmatch(api_key_secret_key)
        ):
            fail(
                "AIPERF_API_KEY_SECRET_KEY must be 1-253 letters, numbers, "
                "dots, underscores, or hyphens"
            )

    image_pull_secret = optional_env("AIPERF_IMAGE_PULL_SECRET")
    if image_pull_secret:
        dns_label("AIPERF_IMAGE_PULL_SECRET", image_pull_secret)

    active_deadline = positive_int("AIPERF_ACTIVE_DEADLINE_SECONDS", "14400")
    models = optional_env("AIPERF_MODELS")

    container_env: list[dict[str, object]] = [
        env_var("ENDPOINT_URL", endpoint_url),
        env_var("ARTIFACTS_DIR", "/artifacts"),
        env_var("RUN_ID", run_id),
        env_var("TURNS", str(positive_int("AIPERF_TURNS", "6"))),
        env_var(
            "DATASET_SESSIONS",
            str(positive_int("AIPERF_DATASET_SESSIONS", "8")),
        ),
        env_var("CONCURRENCY", str(positive_int("AIPERF_CONCURRENCY", "3"))),
        env_var(
            "REQUEST_TIMEOUT_SECONDS",
            str(positive_int("AIPERF_REQUEST_TIMEOUT_SECONDS", "300")),
        ),
        env_var(
            "MODEL_TIMEOUT_SECONDS",
            str(positive_int("AIPERF_MODEL_TIMEOUT_SECONDS", "900")),
        ),
        env_var(
            "SYNTHESIS_TIMEOUT_SECONDS",
            str(positive_int("AIPERF_SYNTHESIS_TIMEOUT_SECONDS", "120")),
        ),
        env_var(
            "DISCOVERY_TIMEOUT_SECONDS",
            str(positive_int("AIPERF_DISCOVERY_TIMEOUT_SECONDS", "10")),
        ),
        env_var("RANDOM_SEED", str(positive_int("AIPERF_RANDOM_SEED", "42"))),
        env_var("TOKENIZER", env("AIPERF_TOKENIZER", "auto")),
        env_var(
            "USE_SERVER_TOKEN_COUNT",
            boolean("AIPERF_USE_SERVER_TOKEN_COUNT", "0"),
        ),
        env_var(
            "USE_LEGACY_MAX_TOKENS",
            boolean("AIPERF_USE_LEGACY_MAX_TOKENS", "1"),
        ),
        env_var("STOP_ON_ERROR", boolean("AIPERF_STOP_ON_ERROR", "0")),
        env_var(
            "CLIENT_VANTAGE",
            f"Kubernetes Job in namespace {namespace}",
        ),
        env_var("HF_HOME", "/tmp/huggingface"),
    ]
    if models:
        container_env.append(env_var("MODELS", models))
    if api_key_secret:
        container_env.append(
            {
                "name": "OPENAI_API_KEY",
                "valueFrom": {
                    "secretKeyRef": {
                        "name": api_key_secret,
                        "key": api_key_secret_key,
                    }
                },
            }
        )

    pod_spec: dict[str, object] = {
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "restartPolicy": "Never",
        "terminationGracePeriodSeconds": 90,
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 65532,
            "runAsGroup": 65532,
            "fsGroup": 65532,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [
            {
                "name": "aiperf",
                "image": image,
                "imagePullPolicy": pull_policy,
                "command": ["/opt/aiperf/run-kubernetes-container.sh"],
                "env": container_env,
                "resources": {
                    "requests": {
                        "cpu": quantity("AIPERF_CPU_REQUEST", "1"),
                        "memory": quantity("AIPERF_MEMORY_REQUEST", "2Gi"),
                        "ephemeral-storage": quantity(
                            "AIPERF_EPHEMERAL_STORAGE_REQUEST", "2Gi"
                        ),
                    },
                    "limits": {
                        "cpu": quantity("AIPERF_CPU_LIMIT", "4"),
                        "memory": quantity("AIPERF_MEMORY_LIMIT", "8Gi"),
                        "ephemeral-storage": quantity(
                            "AIPERF_EPHEMERAL_STORAGE_LIMIT", "24Gi"
                        ),
                    },
                },
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                    "readOnlyRootFilesystem": True,
                },
                "volumeMounts": [
                    {"name": "artifacts", "mountPath": "/artifacts"},
                    {"name": "temporary", "mountPath": "/tmp"},
                ],
            }
        ],
        "volumes": [
            {
                "name": "artifacts",
                "emptyDir": {
                    "sizeLimit": quantity("AIPERF_ARTIFACTS_SIZE_LIMIT", "16Gi")
                },
            },
            {
                "name": "temporary",
                "emptyDir": {
                    "sizeLimit": quantity("AIPERF_TEMP_SIZE_LIMIT", "8Gi")
                },
            },
        ],
    }
    if image_pull_secret:
        pod_spec["imagePullSecrets"] = [{"name": image_pull_secret}]

    name = job_name(run_id)
    manifest = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/name": "daedalus-dynamo-aiperf",
                "app.kubernetes.io/component": "benchmark",
                "app.kubernetes.io/part-of": "daedalus-dynamo-demo",
            },
            "annotations": {"daedalus-dynamo/run-id": run_id},
        },
        "spec": {
            "activeDeadlineSeconds": active_deadline,
            "backoffLimit": 0,
            "template": {
                "metadata": {
                    "labels": {
                        "app.kubernetes.io/name": "daedalus-dynamo-aiperf",
                        "app.kubernetes.io/component": "benchmark",
                    },
                    "annotations": {"daedalus-dynamo/run-id": run_id},
                },
                "spec": pod_spec,
            },
        },
    }
    json.dump(manifest, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
