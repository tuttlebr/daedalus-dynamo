#!/usr/bin/env python3
"""Build single-node, revision-pinned recipes from the recorded HF collection."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import re
from pathlib import Path

import yaml

from model_values import resolve

ROOT = Path(__file__).resolve().parents[1]
GIB = 2**30


def load_snapshot(root: Path = ROOT) -> dict:
    source = json.loads((root / "config/model-catalog/snapshot.json").read_text())
    for model in source["models"]:
        if model.get("config_file"):
            data = gzip.decompress((root / model["config_file"]).read_bytes())
            if hashlib.sha256(data).hexdigest() != model["config_sha256"]:
                raise ValueError(f"Corrupt saved model config: {model['id']}")
            model["config"] = json.loads(data)
    return source


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def builder(model: dict) -> str:
    name = model["id"].split("/")[-1].lower()
    # Builder is the original model family owner, not the quantization publisher.
    for prefix, owner in (("qwen", "qwen"), ("gemma", "google"),
                          ("diffusiongemma", "google"), ("phi", "microsoft"),
                          ("mistral", "mistralai"), ("muse", "meta")):
        if name.startswith(prefix):
            return owner
    if "nemotron" in name:
        return "nvidia"
    if name.startswith("llama"):
        return "meta"
    return slug(model["id"].split("/")[0])


def decoder_config(config: dict) -> dict:
    for key in ("text_config", "llm_config"):
        if isinstance(config.get(key), dict):
            return decoder_config(config[key])
    return config


def kv_bytes_per_token(model: dict, tp: int) -> int | None:
    config = decoder_config(model.get("config") or {})
    heads = config.get("num_key_value_heads", config.get("num_attention_heads"))
    layers = config.get("num_hidden_layers")
    dim = config.get("head_dim")
    if not dim and config.get("hidden_size") and config.get("num_attention_heads"):
        dim = config["hidden_size"] // config["num_attention_heads"]
    if not all(isinstance(x, int) and x > 0 for x in (heads, layers, dim)):
        return None
    # Bound hybrid/sliding attention with full BF16 KV on every decoder layer.
    # Recurrent state and runtime workspace are covered separately by the reserve.
    return 2 * 2 * math.ceil(heads / tp) * dim * layers


def is_moe(model: dict) -> bool:
    config = decoder_config(model.get("config") or {})
    return any(config.get(k, 0) for k in ("num_local_experts", "num_experts", "n_routed_experts"))


def fit(model: dict, hardware: dict, tp: int, max_context: int = 8192) -> dict:
    weights = model["weight_bytes"] / tp
    # Four GiB per rank covers loading/repacking, eager workspace, recurrent
    # state, and replicated auxiliary tensors. This is a screening estimate.
    reserve = 4 * GIB
    capacity = hardware["memory_gib"] * GIB * hardware["memory_fraction"]
    kv_per_token = kv_bytes_per_token(model, tp)
    config = decoder_config(model.get("config") or {})
    model_limit = config.get("max_position_embeddings", 8192)
    context = min(max_context, model_limit if isinstance(model_limit, int) else max_context)
    while context >= 2048:
        kv = kv_per_token * context if kv_per_token else GIB
        required = weights + reserve + kv
        if required <= capacity:
            return {"fits": True, "context_length": context,
                    "weight_gib_per_rank": round(weights / GIB, 3),
                    "runtime_reserve_gib_per_rank": 4,
                    "kv_gib_per_rank": round(kv / GIB, 3),
                    "kv_geometry": "bf16-full-attention-bound" if kv_per_token else "unresolved-1GiB-placeholder",
                    "estimated_gib_per_rank": round(required / GIB, 3),
                    "budget_gib_per_rank": round(capacity / GIB, 3)}
        context //= 2
    return {"fits": False, "weight_gib_per_rank": round(weights / GIB, 3),
            "budget_gib_per_rank": round(capacity / GIB, 3),
            "reason": "weights plus runtime reserve and minimum KV exceed this profile budget"}


def write_yaml(path: Path, document: dict, comment: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((f"# {comment}\n" if comment else "") + yaml.safe_dump(document, sort_keys=False))


def model_options(model: dict) -> dict:
    config = model.get("config") or {}
    options: dict = {"revision": model["revision"]}
    name = model["id"].lower()
    if "nvfp4" in name:
        # Auto-detect mixed ModelOpt schemes; forcing one global quantization
        # scheme is incorrect for Muse and Nemotron Omni.
        options["linear-backend"] = "marlin"
        if is_moe(model) or "scout" in name:
            options["moe-backend"] = "marlin"
    if any(key in config for key in ("vision_config", "audio_config", "llm_config")):
        options["language-model-only"] = True
    # These are text-serving starting points. No speculative/draft model is
    # silently enabled, and no remote model code is executed by this generator.
    return options


def generate(root: Path = ROOT) -> dict:
    source = load_snapshot(root)
    hardware = yaml.safe_load((root / "config/model-catalog/hardware.yaml").read_text())["profiles"]
    entries = []
    for model in source["models"]:
        model_id = model["id"]
        entry = {"id": model_id, "revision": model["revision"],
                 "weight_gib": round(model["weight_bytes"] / GIB, 3),
                 "recipes": [], "assessments": []}
        config = model.get("config") or {}
        architectures = config.get("architectures", [])
        if any("DraftModel" in arch for arch in architectures):
            entry.update(status="draft-only", reason="Speculative draft checkpoint; requires its target model, not a standalone DGD.")
        elif not model["weight_bytes"] or model["pipeline_tag"] in ("text-to-image", "text-to-video", "image-to-image"):
            entry.update(status="requires-diffusion-runtime", reason="Image/video/ONNX artifact; this chart launches Dynamo text/VLM engines, not Diffusers or ONNX pipelines.")
        elif model["weight_bytes"] > max(h["memory_gib"] * h["gpus_per_node"] * GIB for h in hardware.values()):
            entry.update(status="too-large-single-node", reason="Checkpoint weights alone exceed every available single-node memory envelope.")
            if model_id == "nvidia/Qwen3.8-Flash-Next-NVFP4":
                entry.update(status="specialized-offload-recipe", reason="Existing Spark recipe offloads embeddings to local NVMe; not a fully resident profile.")
                entry["recipes"].append("values/qwen/qwen3-8-flash-next-nvfp4/dgx-spark/tp1-pp1-dp1-nvme-offload.yaml")
        else:
            directory = root / "values" / builder(model) / slug(model_id.split("/")[-1])
            base = {"extends": ["../../_shared/vllm.yaml"],
                    "model": {"id": model_id},
                    "modelexpress": {"modelRevision": model["revision"]},
                    "worker": {"vllmOptions": model_options(model)}}
            if (directory / "_tuning.yaml").exists():
                base["extends"].append("_tuning.yaml")
            for hardware_name, hw in hardware.items():
                # Keep measured model/hardware tuning outside generated leaves so
                # rebuilding the catalog cannot restore a failed startup budget.
                tuning_path = directory / hardware_name / "_tuning.yaml"
                tuning = resolve(tuning_path) if tuning_path.exists() else {}
                tuned_options = tuning.get("worker", {}).get("vllmOptions", {})
                fit_hardware = {**hw, "memory_fraction": tuned_options.get("gpu-memory-utilization", hw["memory_fraction"])}
                for tp in (1, 2):
                    if tp > hw["gpus_per_node"]:
                        continue
                    heads = decoder_config(config).get("num_attention_heads")
                    if heads and heads % tp:
                        continue
                    # Offline screening needs a numeric context. It must not cap
                    # the runtime's independently profiled automatic context.
                    screening_context = tuning.get("recipe", {}).get("screeningContextLength", 8192)
                    assessment = fit(model, fit_hardware, tp, screening_context)
                    entry["assessments"].append({"hardware": hardware_name, "tp": tp, **assessment})
                    if not assessment["fits"]:
                        continue
                    identity = f"{model_id}:{hardware_name}:tp{tp}"
                    suffix = hashlib.sha256(identity.encode()).hexdigest()[:6]
                    hw_short = "spark" if hardware_name == "dgx-spark" else hardware_name
                    name = f"mx-{slug(model_id.split('/')[-1])[:22].rstrip('-')}-{hw_short}-tp{tp}-{suffix}"
                    path = directory / hardware_name / f"tp{tp}-pp1-dp1.yaml"
                    options = {"tensor-parallel-size": tp}
                    worker = {"gpu": str(tp), "vllmOptions": options}
                    if hw["unified_memory"] and "memoryRequest" not in tuning.get("worker", {}):
                        worker["memoryRequest"] = f"{math.ceil(assessment['estimated_gib_per_rank']) + 2}Gi"
                    recipe = {"extends": ["../model.yaml", f"../../../_hardware/{hardware_name}.yaml"],
                              "recipe": {"release": name, "hardware": hardware_name,
                                         "parallelism": {"tp": tp, "pp": 1, "dp": 1},
                                         "screeningContextLength": assessment["context_length"],
                                         "qualification": tuning.get("recipe", {}).get("qualification", "candidate-not-gpu-smoke-tested")},
                              "model": {"name": name}, "worker": worker,
                              "modelCache": {"name": name + "-cache", "storage": f"{max(32, math.ceil(model['weight_bytes'] / GIB * 1.2))}Gi"},
                              "redis": {"name": name + "-redis", "dataPvcName": name + "-redis-data"}}
                    # A draft checkpoint shares the cache. Do not replace its
                    # explicitly reserved capacity with the target-only estimate.
                    if "storage" in tuning.get("modelCache", {}):
                        recipe["modelCache"].pop("storage")
                    if tuning_path.exists():
                        recipe["extends"].append("_tuning.yaml")
                    if model["config_status"] in (401, 403):
                        recipe["recipe"]["qualification"] = "requires-huggingface-access-and-memory-validation"
                    write_yaml(path, recipe, "Generated by scripts/build_model_catalog.py; shared settings live in the parent layers.")
                    entry["recipes"].append(str(path.relative_to(root)))
            if entry["recipes"]:
                write_yaml(directory / "model.yaml", base, f"Checkpoint publisher: {model_id.split('/')[0]}; builder: {builder(model)}.")
                entry["status"] = "candidate-recipes"
            else:
                entry.update(status="outside-conservative-memory-budget", reason="No resident candidate fits the declared runtime and KV reserve; offload needs separate qualification.")
        entries.append(entry)
    manifest = {"schema_version": 1, "snapshot": "config/model-catalog/snapshot.json",
                "hardware": "config/model-catalog/hardware.yaml",
                "policy": {"nodes_per_instance": 1, "pp": 1, "dp": 1,
                           "scope": "Generic candidate defaults; NVIDIA Spark profiles override engine settings per recipe.",
                           "replicas": 1, "runtime_max_model_len": "auto",
                           "default_screening_context": 8192,
                           "max_screening_context": max((a.get("context_length", 0) for e in entries for a in e["assessments"]), default=8192),
                           "max_sequences": 1,
                           "kv_dtype": "bfloat16", "performance_qualified": False},
                "models": entries}
    nvidia_source = root / "config/model-catalog/nvidia-spark.json"
    if nvidia_source.exists():
        profiles = json.loads(nvidia_source.read_text())["profiles"]
        manifest["nvidia_spark_recipes"] = {
            "source": "config/model-catalog/nvidia-spark.json",
            "qualification": "requires-image-build-and-gpu-validation",
            "recipes": [p["values"] for p in profiles],
        }
        # The collection snapshot remains an exact historical collection. These
        # separately authored checkpoint recipes are tracked without rewriting it.
        manifest["supplemental_recipes"] = [
            {"id": p["model"], "revision": p["revision"], "recipes": [p["values"]]}
            for p in profiles if p["supplemental"]
        ]
    output = root / "config/model-catalog/recipes.json"
    output.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    manifest = generate()
    counts: dict[str, int] = {}
    for model in manifest["models"]:
        counts[model["status"]] = counts.get(model["status"], 0) + 1
    print(json.dumps({"models": len(manifest["models"]), "recipes": sum(len(m["recipes"]) for m in manifest["models"]), "status": counts}, indent=2))


if __name__ == "__main__":
    main()
