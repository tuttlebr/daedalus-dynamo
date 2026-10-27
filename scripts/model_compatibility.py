"""Resolve the reviewed legacy names to the one active Bolt deployment."""

from __future__ import annotations

import json
import re
from pathlib import Path

CANONICAL_MODEL = "nvidia/NVIDIA-Nemotron-3.5-Bolt-74B-A9B-24GB"
CANONICAL_DGD = "nemotron35bolt-vllm-gb10"
ALIASES = frozenset({"Qwen/Qwen3.6-35B-A3B-FP8", "Qwen/Qwen3.8-27B-FP8"})
POLICY_PATH = Path(__file__).resolve().parents[1] / "config/model-compatibility.json"


def load_policy(path: Path = POLICY_PATH) -> dict:
    policy = json.loads(path.read_text())
    if (
        not isinstance(policy, dict)
        or set(policy)
        != {
            "schema_version",
            "canonical_model",
            "canonical_revision",
            "backend_dgd",
            "aliases",
        }
        or type(policy["schema_version"]) is not int
        or policy["schema_version"] != 1
        or policy["canonical_model"] != CANONICAL_MODEL
        or policy["backend_dgd"] != CANONICAL_DGD
        or not isinstance(policy["canonical_revision"], str)
        or re.fullmatch(r"[a-f0-9]{40}", policy["canonical_revision"]) is None
        or not isinstance(policy["aliases"], list)
        or len(policy["aliases"]) != len(ALIASES)
        or any(not isinstance(alias, str) for alias in policy["aliases"])
        or set(policy["aliases"]) != ALIASES
        or policy["canonical_model"] in policy["aliases"]
    ):
        raise ValueError("Invalid or unreviewed model compatibility policy")
    return policy


def catalog_metadata(model_id: str, annotations: dict, policy: dict) -> dict:
    declared = annotations.get("daedalus-dynamo/compatibility-alias") == "true"
    if model_id not in policy["aliases"]:
        if declared:
            raise ValueError("Unreviewed compatibility model ID")
        return {}
    if (
        not declared
        or annotations.get("daedalus-dynamo/model-dgd") != policy["backend_dgd"]
        or annotations.get("daedalus-dynamo/canonical-model-id")
        != policy["canonical_model"]
        or annotations.get("daedalus-dynamo/canonical-model-revision")
        != policy["canonical_revision"]
    ):
        raise ValueError(
            "Compatibility route does not identify the active Bolt deployment"
        )
    return {
        "compatibility_alias": True,
        "canonical_model": policy["canonical_model"],
        "model_revision": policy["canonical_revision"],
    }
