#!/usr/bin/env python3
"""Run offline AISimulate predictions in the pinned Planner image; no GPUs used."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import subprocess
from pathlib import Path

import yaml

from build_model_catalog import load_snapshot
from model_values import ROOT, resolve

PLANNER = "nvcr.io/nvidia/ai-dynamo/dynamo-planner:1.6.1@sha256:7a3f07ce406c988c3c7ce30578fa1880831bb9408454daafdcb0a6f7ad1dabff"


def prediction(model: dict, values: dict, config_path: str) -> dict:
    options = values["worker"]["vllmOptions"]
    recipe = values["recipe"]
    tp = recipe["parallelism"]["tp"]
    timing = {"kvcache_quant_mode": "bfloat16"}
    quant = (model.get("config") or {}).get("quantization_config", {})
    mixed = "mixed" in str(quant).lower()
    if not mixed:
        if "NVFP4" in model["id"]:
            # AISimulate's explicit W4A16 lane uses BF16 compute. Native
            # 'nvfp4' would incorrectly require FP4 tensor cores on L40S/A30.
            timing.update(gemm_quant_mode="w4a16_nvfp4", moe_quant_mode="w4a16_nvfp4")
        elif "FP8" in model["id"]:
            timing.update(gemm_quant_mode="fp8", moe_quant_mode="fp8")
    context = options["max-model-len"]
    if context in ("auto", -1):
        # AISimulate runs without a GPU and cannot profile vLLM's serving limit.
        # Use the catalog's numeric screening workload only for this estimate.
        context = recipe["screeningContextLength"]
    return {
        "engine": {
            "model": config_path,
            "hardware": "gb10" if recipe["hardware"] == "dgx-spark" else recipe["hardware"],
            "backend": "vllm", "backend_version": "current", "mode": "aggregated",
            "context_length": context, "estimation_mode": "op_level", "database_mode": "HYBRID",
            "workers": {"aggregated": {
                "parallelism": {"tensor": tp, "pipeline": 1, "attention_data": 1,
                                "moe_tensor": tp, "moe_expert": 1, "replicas": 1},
                "scheduler": {"max_batched_tokens": options["max-num-batched-tokens"], "max_sequences": 1},
                "kv_cache": {"prefix_caching": True, "capacity": {"memory_fraction": options["gpu-memory-utilization"]}},
                "timing": timing,
            }},
        },
        "traffic": {"source": {"type": "synthetic", "input_tokens": min(4096, context - 512), "output_tokens": 512},
                    "load": {"type": "concurrency", "concurrency": 1}, "stop": {"requests": 4}},
        "evaluation": {"sla": {"ttft_ms": 1500.0, "itl_ms": 75.0}},
    }


def simulate(model: dict, path: str, output: Path, image: str, timeout: int) -> dict:
    values = resolve(ROOT / path)
    release = values["recipe"]["release"]
    target = output / release
    target.mkdir(parents=True, exist_ok=True)
    record = {"recipe": path, "model": model["id"], "revision": model["revision"],
              "hardware": values["recipe"]["hardware"], "tp": values["recipe"]["parallelism"]["tp"],
              "runtime_vllm_version": "0.31.0", "performance_qualified": False}
    record_path = target / "outcome.json"
    if record_path.exists():
        return json.loads(record_path.read_text())
    if not model.get("config"):
        record.update(status="missing-model-config", reason=f"HF config returned HTTP {model['config_status']}; access must be granted before simulation.")
    else:
        model_dir = target / "model"
        model_dir.mkdir(exist_ok=True)
        (model_dir / "config.json").write_text(json.dumps(model["config"], indent=2) + "\n")
        container_dir = "/work/" + str(target.relative_to(ROOT))
        config = prediction(model, values, container_dir + "/model")
        (target / "predict.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
        container_name = "dynamo-sim-" + hashlib.sha256(str(target).encode()).hexdigest()[:16]
        command = ["docker", "run", "--rm", "--name", container_name, "--memory", "6g", "--cpus", "2",
                   "-e", "HF_HUB_OFFLINE=1", "-v", f"{ROOT}:/work",
                   "--entrypoint", "/opt/dynamo/venv/bin/aisimulate", image,
                   "predict", "--stack", "engine", "-c", container_dir + "/predict.yaml",
                   "--format", "json", "--detail", "memory,source",
                   "--output-dir", container_dir + "/result", "--overwrite"]
        record["command"] = command
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
            (target / "stdout.log").write_text(completed.stdout)
            (target / "stderr.log").write_text(completed.stderr)
            data = None
            for line in completed.stdout.splitlines():
                if line.startswith("{"):
                    try:
                        candidate = json.loads(line)
                        if "summary" in candidate:
                            data = candidate
                    except ValueError:
                        continue
            if completed.returncode == 0 and data:
                summary = data["summary"]
                record.update(status="predicted-estimate", summary={k: summary.get(k) for k in (
                    "completed_requests", "mean_ttft_ms", "mean_tpot_ms", "output_throughput_tok_s",
                    "prediction_provenance", "kv_cache_precision")})
                (target / "prediction.json").write_text(json.dumps(data, indent=2) + "\n")
            else:
                record.update(status="unsupported-or-failed", exit_code=completed.returncode,
                              reason=(completed.stderr or completed.stdout)[-3500:])
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, check=False)
            record.update(status="timeout", reason=f"Prediction exceeded {timeout} seconds.")
    record_path.write_text(json.dumps(record, indent=2) + "\n")
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/model-catalog-aisimulate")
    parser.add_argument("--image", default=PLANNER)
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=90)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT):
        parser.error("output must be inside this checkout for the container mount")
    snapshot = load_snapshot()
    models = {m["id"]: m for m in snapshot["models"]}
    manifest = json.loads((ROOT / "config/model-catalog/recipes.json").read_text())
    work = [(models[m["id"]], p) for m in manifest["models"] if m["status"] == "candidate-recipes" for p in m["recipes"]]
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as executor:
        futures = [executor.submit(simulate, m, p, output, args.image, args.timeout) for m, p in work]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            results.append(result)
            print(result["recipe"], result["status"], flush=True)
    report = {"planner_image": args.image, "aisimulate_version": "0.13.0", "execution": "CPU-only offline engine",
              "qualification": "HYBRID estimates, not GPU measurements; simulator data version differs from runtime",
              "workload": "4096 input / 512 output tokens, concurrency 1; input shortened for smaller context",
              "results": sorted(results, key=lambda r: r["recipe"])}
    (output / "summary.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
