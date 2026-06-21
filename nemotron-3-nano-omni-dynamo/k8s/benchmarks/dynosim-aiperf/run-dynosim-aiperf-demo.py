#!/usr/bin/env python3

import csv
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
from collections.abc import Iterable
from typing import Any


def env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def env_int(name: str, default: int) -> int:
    value = env(name, str(default))
    try:
        return int(value)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {value!r}") from exc


def env_float(name: str, default: float) -> float:
    value = env(name, str(default))
    try:
        return float(value)
    except ValueError as exc:
        raise SystemExit(f"{name} must be a float, got {value!r}") from exc


def env_bool(name: str, default: bool) -> bool:
    value = env(name, str(default).lower()).lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off", ""}:
        return False
    raise SystemExit(f"{name} must be true or false, got {value!r}")


def env_optional_int(name: str) -> int | None:
    value = env(name, "")
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {value!r}") from exc


def env_optional_float(name: str) -> float | None:
    value = env(name, "")
    if not value:
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise SystemExit(f"{name} must be a float, got {value!r}") from exc


def env_json_object(name: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
    value = env(name, "")
    if not value:
        return dict(default or {})
    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{name} must be a JSON object, got {value!r}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"{name} must be a JSON object, got {type(data).__name__}")
    return data


def env_list(name: str, default: str) -> list[str]:
    raw = env(name, default).replace(",", " ")
    return [item for item in (part.strip() for part in raw.split()) if item]


def env_ratios(name: str, default: str) -> list[float]:
    ratios = []
    for item in env_list(name, default):
        try:
            ratio = float(item)
        except ValueError as exc:
            raise SystemExit(f"{name} contains non-float value {item!r}") from exc
        if ratio < 0 or ratio >= 1:
            raise SystemExit(f"{name} ratios must be >= 0 and < 1, got {ratio}")
        ratios.append(ratio)
    if not ratios:
        raise SystemExit(f"{name} must contain at least one ratio")
    return ratios


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def flatten(data: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(data, dict):
        flat: dict[str, Any] = {}
        for key, value in data.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            flat.update(flatten(value, child_prefix))
        return flat
    if isinstance(data, list):
        return {}
    return {prefix: data}


def pick_metric(report: dict[str, Any], names: Iterable[str]) -> Any:
    flat = flatten(report)
    for name in names:
        if name in flat:
            return flat[name]
    for name in names:
        for key, value in flat.items():
            if key.rsplit(".", 1)[-1] == name:
                return value
    return None


def find_profile_export(artifact_dir: pathlib.Path) -> pathlib.Path | None:
    candidates = sorted(artifact_dir.rglob("profile_export_aiperf.json"))
    if candidates:
        return candidates[0]
    candidates = sorted(artifact_dir.rglob("*profile*aiperf*.json"))
    return candidates[0] if candidates else None


def load_json(path: pathlib.Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    with path.open() as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def metric_value(result: dict[str, Any], metric: str, percentile: str) -> Any:
    value = result.get(metric)
    if isinstance(value, dict):
        return value.get(percentile)
    return None


def metric_number(value: Any) -> float | None:
    if isinstance(value, dict):
        value = value.get("avg")
    if isinstance(value, int | float):
        return float(value)
    return None


def ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def sanitize_k8s_name(value: str, default: str) -> str:
    name = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    name = re.sub(r"-+", "-", name)
    if not name:
        name = default
    return name[:63].strip("-") or default


def yaml_scalar(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, int | float):
        return str(value)
    return json.dumps(str(value))


def to_yaml(data: Any, indent: int = 0) -> str:
    spaces = " " * indent
    if isinstance(data, dict):
        lines: list[str] = []
        for key, value in data.items():
            if isinstance(value, dict | list):
                lines.append(f"{spaces}{key}:")
                lines.append(to_yaml(value, indent + 2))
            else:
                lines.append(f"{spaces}{key}: {yaml_scalar(value)}")
        return "\n".join(lines)
    if isinstance(data, list):
        lines = []
        for item in data:
            if isinstance(item, dict):
                lines.append(f"{spaces}-")
                lines.append(to_yaml(item, indent + 2))
            elif isinstance(item, list):
                lines.append(f"{spaces}-")
                lines.append(to_yaml(item, indent + 2))
            else:
                lines.append(f"{spaces}- {yaml_scalar(item)}")
        return "\n".join(lines)
    return f"{spaces}{yaml_scalar(data)}"


def write_rows(path: pathlib.Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps(rows, indent=2, sort_keys=True) + "\n")


def write_csv(path: pathlib.Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def run_checked(cmd: list[str]) -> None:
    print(" ".join(shlex.quote(part) for part in cmd), flush=True)
    subprocess.run(cmd, check=True)


def default_dynosim_engine_args(concurrency: int) -> dict[str, Any]:
    return {
        "engine_type": "vllm",
        "block_size": 64,
        "enable_prefix_caching": True,
        "enable_chunked_prefill": True,
        "max_num_seqs": concurrency,
        "max_num_batched_tokens": 2048,
    }


def ensure_dynosim_available() -> None:
    cmd = [sys.executable, "-c", "import dynamo.replay"]
    result = subprocess.run(cmd, text=True, capture_output=True)
    if result.returncode == 0:
        return
    message = result.stderr.strip() or result.stdout.strip()
    raise SystemExit(
        "DynoSim is not importable in this image. Use a Dynamo runtime/source "
        f"image that includes `python -m dynamo.replay`. Import error: {message}"
    )


def build_extra_inputs(osl: int) -> dict[str, Any]:
    extra_inputs: dict[str, Any] = {}
    if env_bool("DYNAMO_IGNORE_EOS", True):
        extra_inputs["ignore_eos"] = True

    nvext: dict[str, Any] = {}
    extra_fields = env_list("DYNAMO_EXTRA_FIELDS", "worker_id,timing")
    if extra_fields:
        nvext["extra_fields"] = extra_fields

    if env_bool("DYNAMO_AGENT_HINTS_ENABLED", True):
        hints: dict[str, Any] = {}
        if env_bool("USE_EXPECTED_OSL", True):
            hints["osl"] = osl
        priority = env_optional_int("REQUEST_PRIORITY")
        if priority is not None:
            hints["priority"] = priority
        if env_bool("SPECULATIVE_PREFILL", False):
            hints["speculative_prefill"] = True
        hints = deep_merge(hints, env_json_object("DYNAMO_AGENT_HINTS_JSON"))
        if hints:
            nvext["agent_hints"] = hints

    ttl = env("CACHE_CONTROL_TTL", "1h")
    if ttl:
        nvext["cache_control"] = {"type": "ephemeral", "ttl": ttl}
    cache_control = env_json_object("DYNAMO_CACHE_CONTROL_JSON")
    if cache_control:
        base_cache_control = nvext.get("cache_control")
        if base_cache_control is not None and not isinstance(base_cache_control, dict):
            raise SystemExit("nvext.cache_control must be an object")
        nvext["cache_control"] = deep_merge(base_cache_control or {}, cache_control)

    nvext = deep_merge(nvext, env_json_object("DYNAMO_NVEXT_JSON"))
    if nvext:
        extra_inputs["nvext"] = nvext

    return deep_merge(extra_inputs, env_json_object("DYNAMO_EXTRA_INPUTS_JSON"))


def dynosim_metrics(
    report: dict[str, Any],
    prefix_ratio: float,
    router_mode: str,
    report_json: pathlib.Path,
    num_workers: int,
) -> dict[str, Any]:
    return {
        "source": "dynosim",
        "prefix_ratio": prefix_ratio,
        "router_mode": router_mode,
        "num_workers": num_workers,
        "report_json": str(report_json),
        "request_count": pick_metric(report, ["request_count", "requests_completed", "num_requests"]),
        "input_tokens_total": pick_metric(report, ["input_tokens_total", "total_input_tokens"]),
        "output_tokens_total": pick_metric(report, ["output_tokens_total", "total_output_tokens"]),
        "virtual_duration_s": pick_metric(report, ["virtual_duration_s", "duration_s"]),
        "request_throughput_req_s": pick_metric(
            report,
            ["request_throughput_req_s", "request_throughput", "requests_per_second"],
        ),
        "output_throughput_tok_s": pick_metric(
            report,
            ["output_throughput_tok_s", "output_token_throughput", "output_tokens_per_second"],
        ),
        "prefix_cache_reused_ratio": pick_metric(
            report,
            ["prefix_cache_reused_ratio", "prefix_cache_hit_ratio", "cache_hit_ratio"],
        ),
        "mean_ttft_ms": pick_metric(report, ["mean_ttft_ms", "ttft_mean_ms"]),
        "p50_ttft_ms": pick_metric(report, ["p50_ttft_ms", "ttft_p50_ms"]),
        "p90_ttft_ms": pick_metric(report, ["p90_ttft_ms", "ttft_p90_ms"]),
        "mean_tpot_ms": pick_metric(report, ["mean_tpot_ms", "tpot_mean_ms"]),
        "mean_itl_ms": pick_metric(report, ["mean_itl_ms", "itl_mean_ms"]),
        "mean_e2e_latency_ms": pick_metric(
            report,
            ["mean_e2e_latency_ms", "mean_request_latency_ms", "request_latency_mean_ms"],
        ),
        "p90_e2e_latency_ms": pick_metric(
            report,
            ["p90_e2e_latency_ms", "p90_request_latency_ms", "request_latency_p90_ms"],
        ),
    }


def aiperf_metrics(
    result: dict[str, Any],
    prefix_ratio: float,
    artifact_dir: pathlib.Path,
    profile_export: pathlib.Path | None,
) -> dict[str, Any]:
    return {
        "source": "aiperf",
        "prefix_ratio": prefix_ratio,
        "router_mode": "live_dynamo",
        "artifact_dir": str(artifact_dir),
        "profile_export": str(profile_export) if profile_export else "",
        "ttft_p50_ms": metric_value(result, "time_to_first_token", "p50"),
        "ttft_p90_ms": metric_value(result, "time_to_first_token", "p90"),
        "itl_p50_ms": metric_value(result, "inter_token_latency", "p50"),
        "itl_p90_ms": metric_value(result, "inter_token_latency", "p90"),
        "request_latency_p50_ms": metric_value(result, "request_latency", "p50"),
        "request_latency_p90_ms": metric_value(result, "request_latency", "p90"),
        "output_throughput_tok_s": metric_number(result.get("output_token_throughput")),
        "request_throughput_req_s": metric_number(result.get("request_throughput")),
    }


def best_by(
    rows: list[dict[str, Any]],
    key: str,
    reverse: bool = False,
) -> dict[str, Any] | None:
    candidates = [row for row in rows if metric_number(row.get(key)) is not None]
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda row: metric_number(row.get(key)) or 0.0,
        reverse=reverse,
    )[0]


def dynosim_router_modes_identical(rows: list[dict[str, Any]]) -> bool:
    metrics = (
        "mean_ttft_ms",
        "mean_itl_ms",
        "mean_e2e_latency_ms",
        "output_throughput_tok_s",
        "prefix_cache_reused_ratio",
    )
    by_prefix: dict[float, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("source") != "dynosim":
            continue
        prefix_ratio = metric_number(row.get("prefix_ratio"))
        if prefix_ratio is None:
            continue
        by_prefix.setdefault(prefix_ratio, []).append(row)

    for prefix_rows in by_prefix.values():
        if len(prefix_rows) < 2:
            continue
        reference = prefix_rows[0]
        for row in prefix_rows[1:]:
            for metric in metrics:
                left = metric_number(reference.get(metric))
                right = metric_number(row.get(metric))
                if left is None and right is None:
                    continue
                if left is None or right is None or abs(left - right) > 1e-9:
                    return False
    return True


def build_decision_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    live_rows = sorted(
        [row for row in rows if row.get("source") == "aiperf"],
        key=lambda row: metric_number(row.get("prefix_ratio")) or 0.0,
    )
    dynosim_rows = [row for row in rows if row.get("source") == "dynosim"]
    dynosim_by_prefix: dict[float, dict[str, Any]] = {}
    for row in dynosim_rows:
        prefix_ratio = metric_number(row.get("prefix_ratio"))
        if prefix_ratio is None:
            continue
        if row.get("router_mode") == "kv_router" or prefix_ratio not in dynosim_by_prefix:
            dynosim_by_prefix[prefix_ratio] = row

    baseline_live = live_rows[0] if live_rows else None
    baseline_ttft_p90 = metric_number(baseline_live.get("ttft_p90_ms")) if baseline_live else None
    baseline_latency_p90 = (
        metric_number(baseline_live.get("request_latency_p90_ms")) if baseline_live else None
    )
    baseline_output_throughput = (
        metric_number(baseline_live.get("output_throughput_tok_s")) if baseline_live else None
    )

    prefix_rows: list[dict[str, Any]] = []
    for live_row in live_rows:
        prefix_ratio = metric_number(live_row.get("prefix_ratio"))
        if prefix_ratio is None:
            continue
        sim_row = dynosim_by_prefix.get(prefix_ratio, {})
        live_ttft_p90 = metric_number(live_row.get("ttft_p90_ms"))
        live_latency_p90 = metric_number(live_row.get("request_latency_p90_ms"))
        live_output_throughput = metric_number(live_row.get("output_throughput_tok_s"))
        prefix_rows.append(
            {
                "prefix_ratio": prefix_ratio,
                "dynosim_prefix_cache_reused_ratio": metric_number(
                    sim_row.get("prefix_cache_reused_ratio")
                ),
                "dynosim_mean_ttft_ms": metric_number(sim_row.get("mean_ttft_ms")),
                "dynosim_mean_e2e_latency_ms": metric_number(
                    sim_row.get("mean_e2e_latency_ms")
                ),
                "dynosim_output_throughput_tok_s": metric_number(
                    sim_row.get("output_throughput_tok_s")
                ),
                "live_ttft_p90_ms": live_ttft_p90,
                "live_request_latency_p90_ms": live_latency_p90,
                "live_output_throughput_tok_s": live_output_throughput,
                "live_ttft_p90_speedup_vs_lowest_prefix": ratio(
                    baseline_ttft_p90,
                    live_ttft_p90,
                ),
                "live_request_latency_p90_speedup_vs_lowest_prefix": ratio(
                    baseline_latency_p90,
                    live_latency_p90,
                ),
                "live_output_throughput_speedup_vs_lowest_prefix": ratio(
                    live_output_throughput,
                    baseline_output_throughput,
                ),
            }
        )

    best_ttft = best_by(live_rows, "ttft_p90_ms")
    best_latency = best_by(live_rows, "request_latency_p90_ms")
    best_throughput = best_by(live_rows, "output_throughput_tok_s", reverse=True)

    return {
        "best_live_prefix_ratio_by_p90_ttft": (
            best_ttft.get("prefix_ratio") if best_ttft else None
        ),
        "best_live_prefix_ratio_by_p90_request_latency": (
            best_latency.get("prefix_ratio") if best_latency else None
        ),
        "best_live_prefix_ratio_by_output_throughput": (
            best_throughput.get("prefix_ratio") if best_throughput else None
        ),
        "router_mode_note": (
            "DynoSim round_robin and kv_router were identical in this run. "
            "Treat this as a prefix-cache sensitivity run, not as proof that "
            "the router policy is irrelevant."
            if dynosim_router_modes_identical(dynosim_rows)
            else "DynoSim router modes differed; inspect per-prefix rows before choosing a router policy."
        ),
        "prefix_rows": prefix_rows,
    }


def print_decision_summary(summary: dict[str, Any]) -> None:
    print("\n=== Decision summary ===", flush=True)
    print(
        "best_live_prefix_ratio_by_p90_ttft="
        f"{summary.get('best_live_prefix_ratio_by_p90_ttft')}",
        flush=True,
    )
    print(
        "best_live_prefix_ratio_by_p90_request_latency="
        f"{summary.get('best_live_prefix_ratio_by_p90_request_latency')}",
        flush=True,
    )
    print(
        "best_live_prefix_ratio_by_output_throughput="
        f"{summary.get('best_live_prefix_ratio_by_output_throughput')}",
        flush=True,
    )
    print(summary.get("router_mode_note"), flush=True)
    kubernetes_spec = summary.get("kubernetes_spec")
    if isinstance(kubernetes_spec, dict):
        print(f"kubernetes_dgdr_yaml={kubernetes_spec.get('yaml')}", flush=True)
    for row in summary.get("prefix_rows", []):
        print(json.dumps(row, sort_keys=True), flush=True)


def best_dynosim_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    dynosim_rows = [row for row in rows if row.get("source") == "dynosim"]
    best_latency = best_by(dynosim_rows, "p90_e2e_latency_ms")
    if best_latency:
        return best_latency
    return best_by(dynosim_rows, "mean_e2e_latency_ms")


def row_for_prefix(
    rows: list[dict[str, Any]],
    source: str,
    prefix_ratio: float | None,
) -> dict[str, Any] | None:
    if prefix_ratio is None:
        return None
    for row in rows:
        if row.get("source") != source:
            continue
        if metric_number(row.get("prefix_ratio")) == prefix_ratio:
            return row
    return None


def selected_prefix_ratio(
    rows: list[dict[str, Any]],
    decision_summary: dict[str, Any],
) -> float | None:
    for key in (
        "best_live_prefix_ratio_by_p90_request_latency",
        "best_live_prefix_ratio_by_p90_ttft",
        "best_live_prefix_ratio_by_output_throughput",
    ):
        value = metric_number(decision_summary.get(key))
        if value is not None:
            return value
    dynosim_row = best_dynosim_row(rows)
    return metric_number(dynosim_row.get("prefix_ratio")) if dynosim_row else None


def add_optional(mapping: dict[str, Any], key: str, value: Any) -> None:
    if value not in (None, ""):
        mapping[key] = value


def build_dgdr_spec(
    rows: list[dict[str, Any]],
    decision_summary: dict[str, Any],
    isl: int,
    osl: int,
    concurrency: int,
) -> dict[str, Any]:
    model = env("MODEL_NAME", "nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-NVFP4")
    default_name = sanitize_k8s_name(model.rsplit("/", 1)[-1], "dynosim-candidate")
    name = sanitize_k8s_name(env("DGDR_NAME", f"{default_name}-dgdr"), "dynosim-candidate")
    best_prefix = selected_prefix_ratio(rows, decision_summary)
    live_row = row_for_prefix(rows, "aiperf", best_prefix)
    dynosim_row = row_for_prefix(rows, "dynosim", best_prefix)

    annotations: dict[str, str] = {
        "dynosim-aiperf.nvidia.com/generated-by": "dynosim-aiperf-demo",
    }
    if best_prefix is not None:
        annotations["dynosim-aiperf.nvidia.com/selected-prefix-ratio"] = f"{best_prefix:g}"
    if live_row:
        for annotation, metric in (
            ("live-p90-ttft-ms", "ttft_p90_ms"),
            ("live-p90-request-latency-ms", "request_latency_p90_ms"),
            ("live-output-throughput-tok-s", "output_throughput_tok_s"),
            ("live-request-throughput-req-s", "request_throughput_req_s"),
        ):
            value = metric_number(live_row.get(metric))
            if value is not None:
                annotations[f"dynosim-aiperf.nvidia.com/{annotation}"] = f"{value:.6g}"
    if dynosim_row:
        value = metric_number(dynosim_row.get("prefix_cache_reused_ratio"))
        if value is not None:
            annotations["dynosim-aiperf.nvidia.com/dynosim-prefix-cache-reused-ratio"] = (
                f"{value:.6g}"
            )

    metadata: dict[str, Any] = {
        "name": name,
        "labels": {
            "app.kubernetes.io/name": name,
            "app.kubernetes.io/part-of": "nemotron-omni-photo-demo",
            "app.kubernetes.io/component": "dynamo-dgdr",
        },
        "annotations": annotations,
    }
    add_optional(metadata, "namespace", env("DGDR_NAMESPACE", ""))

    spec: dict[str, Any] = {
        "model": model,
        "backend": env("DGDR_BACKEND", "vllm"),
        "searchStrategy": env("DGDR_SEARCH_STRATEGY", "rapid"),
        "autoApply": env_bool("DGDR_AUTO_APPLY", False),
    }
    add_optional(spec, "image", env("DGDR_IMAGE", "nvcr.io/nvidia/ai-dynamo/dynamo-planner:1.1.1"))

    workload: dict[str, Any] = {
        "isl": env_optional_int("DGDR_WORKLOAD_ISL") or isl,
        "osl": env_optional_int("DGDR_WORKLOAD_OSL") or osl,
    }
    dgdr_concurrency = env_optional_int("DGDR_CONCURRENCY")
    if dgdr_concurrency is not None:
        workload["concurrency"] = dgdr_concurrency
    elif env_bool("DGDR_INCLUDE_CONCURRENCY", True):
        workload["concurrency"] = concurrency

    request_rate = env_optional_float("DGDR_REQUEST_RATE")
    if request_rate is None and env_bool("DGDR_USE_AIPERF_REQUEST_RATE", False) and live_row:
        request_rate = metric_number(live_row.get("request_throughput_req_s"))
    add_optional(workload, "requestRate", request_rate)
    spec["workload"] = workload

    sla: dict[str, Any] = {}
    e2e_latency = env_optional_int("DGDR_E2E_LATENCY_MS")
    if e2e_latency is not None:
        sla["e2eLatency"] = e2e_latency
    else:
        add_optional(sla, "ttft", env_optional_int("DGDR_TTFT_MS"))
        add_optional(sla, "itl", env_optional_int("DGDR_ITL_MS"))
    if sla:
        spec["sla"] = sla

    hardware: dict[str, Any] = {}
    add_optional(hardware, "gpuSku", env("DGDR_HARDWARE_GPU_SKU", ""))
    add_optional(hardware, "totalGpus", env_optional_int("DGDR_HARDWARE_TOTAL_GPUS"))
    add_optional(
        hardware,
        "numGpusPerNode",
        env_optional_int("DGDR_HARDWARE_NUM_GPUS_PER_NODE"),
    )
    add_optional(hardware, "vramMb", env_optional_int("DGDR_HARDWARE_VRAM_MB"))
    add_optional(hardware, "interconnect", env("DGDR_HARDWARE_INTERCONNECT", ""))
    rdma = env("DGDR_HARDWARE_RDMA", "")
    if rdma:
        hardware["rdma"] = env_bool("DGDR_HARDWARE_RDMA", False)
    if hardware:
        spec["hardware"] = hardware

    model_cache: dict[str, Any] = {}
    add_optional(model_cache, "pvcName", env("DGDR_MODEL_CACHE_PVC_NAME", ""))
    add_optional(
        model_cache,
        "pvcMountPath",
        env("DGDR_MODEL_CACHE_PVC_MOUNT_PATH", ""),
    )
    add_optional(
        model_cache,
        "pvcModelPath",
        env("DGDR_MODEL_CACHE_PVC_MODEL_PATH", ""),
    )
    if model_cache:
        spec["modelCache"] = model_cache

    features: dict[str, Any] = {}
    if env_bool("DGDR_PLANNER_ENABLED", False):
        planner = {"enabled": True}
        planner = deep_merge(planner, env_json_object("DGDR_PLANNER_JSON"))
        features["planner"] = planner
    if features:
        spec["features"] = features

    overrides: dict[str, Any] = {}
    profiling_job_override = env_json_object("DGDR_OVERRIDES_PROFILING_JOB_JSON")
    if profiling_job_override:
        overrides["profilingJob"] = profiling_job_override
    dgd_override = env_json_object("DGDR_OVERRIDES_DGD_JSON")
    if dgd_override:
        overrides["dgd"] = dgd_override
    if overrides:
        spec["overrides"] = overrides

    return {
        "apiVersion": "nvidia.com/v1alpha1",
        "kind": "DynamoGraphDeploymentRequest",
        "metadata": metadata,
        "spec": spec,
    }


def write_kubernetes_spec_artifacts(
    output_dir: pathlib.Path,
    rows: list[dict[str, Any]],
    decision_summary: dict[str, Any],
    isl: int,
    osl: int,
    concurrency: int,
) -> dict[str, str]:
    kubernetes_dir = output_dir / "kubernetes"
    kubernetes_dir.mkdir(parents=True, exist_ok=True)
    dgdr = build_dgdr_spec(rows, decision_summary, isl, osl, concurrency)
    yaml_path = kubernetes_dir / "dynamo-graph-deployment-request.yaml"
    json_path = kubernetes_dir / "dynamo-graph-deployment-request.json"
    readme_path = kubernetes_dir / "README.md"
    yaml_path.write_text(to_yaml(dgdr) + "\n")
    json_path.write_text(json.dumps(dgdr, indent=2, sort_keys=False) + "\n")
    name = dgdr["metadata"]["name"]
    readme_path.write_text(
        "\n".join(
            [
                "# Kubernetes Spec",
                "",
                "This directory contains a reviewable DynamoGraphDeploymentRequest",
                "generated from the DynoSim + AIPerf demo settings and results.",
                "",
                "Apply it into the Dynamo namespace:",
                "",
                "```bash",
                "kubectl apply -n ${NAMESPACE} -f dynamo-graph-deployment-request.yaml",
                f"kubectl get dgdr -n ${{NAMESPACE}} {name} -w",
                "```",
                "",
                "Because `autoApply` defaults to `false`, the operator stops after",
                "profiling and stores the generated DynamoGraphDeployment here:",
                "",
                "```bash",
                f"kubectl get dgdr -n ${{NAMESPACE}} {name} \\",
                "  -o jsonpath='{.status.profilingResults.selectedConfig}' \\",
                "  | python3 -m json.tool > generated-dgd.json",
                "```",
                "",
                "Review `generated-dgd.json`, then apply it if it is the config you",
                "want to deploy:",
                "",
                "```bash",
                "kubectl apply -n ${NAMESPACE} -f generated-dgd.json",
                "```",
                "",
            ]
        )
    )
    return {
        "yaml": str(yaml_path),
        "json": str(json_path),
        "readme": str(readme_path),
    }


def run_dynosim(
    output_dir: pathlib.Path,
    prefix_ratios: list[float],
    isl: int,
    osl: int,
    request_count: int,
    concurrency: int,
    num_prefix_groups: int,
) -> list[dict[str, Any]]:
    ensure_dynosim_available()

    num_workers = env_int("DYNOSIM_NUM_WORKERS", 2)
    replay_mode = env("DYNOSIM_REPLAY_MODE", "offline")
    router_modes = env_list("DYNOSIM_ROUTER_MODES", "round_robin,kv_router")
    arrival_interval_ms = env_float("DYNOSIM_ARRIVAL_INTERVAL_MS", 0.0)
    turns_per_session = env_int("DYNOSIM_TURNS_PER_SESSION", 1)
    inter_turn_delay_ms = env_float("DYNOSIM_INTER_TURN_DELAY_MS", 0.0)
    engine_args = env_json_object(
        "DYNOSIM_ENGINE_ARGS_JSON",
        default_dynosim_engine_args(concurrency),
    )
    router_config = env_json_object(
        "DYNOSIM_ROUTER_CONFIG_JSON",
        {"router_queue_policy": "fcfs"},
    )

    rows: list[dict[str, Any]] = []
    for prefix_ratio in prefix_ratios:
        for router_mode in router_modes:
            if router_mode == "kv_router" and num_workers < 2:
                print(
                    "Skipping DynoSim kv_router run because DYNOSIM_NUM_WORKERS < 2",
                    flush=True,
                )
                continue

            run_dir = output_dir / "dynosim" / f"prefix_ratio_{prefix_ratio:g}" / router_mode
            run_dir.mkdir(parents=True, exist_ok=True)
            report_json = run_dir / "report.json"
            stdout_log = run_dir / "stdout.log"

            cmd = [
                sys.executable,
                "-m",
                "dynamo.replay",
                "--input-tokens",
                str(isl),
                "--output-tokens",
                str(osl),
                "--request-count",
                str(request_count),
                "--shared-prefix-ratio",
                str(prefix_ratio),
                "--num-prefix-groups",
                str(num_prefix_groups),
                "--replay-mode",
                replay_mode,
                "--router-mode",
                router_mode,
                "--num-workers",
                str(num_workers),
                "--replay-concurrency",
                str(concurrency),
                "--extra-engine-args",
                json.dumps(engine_args, separators=(",", ":")),
                "--report-json",
                str(report_json),
            ]

            if router_mode == "kv_router" and router_config:
                cmd.extend(
                    [
                        "--router-config",
                        json.dumps(router_config, separators=(",", ":")),
                    ]
                )
            if arrival_interval_ms > 0:
                cmd.extend(["--arrival-interval-ms", str(arrival_interval_ms)])
            if turns_per_session > 1:
                cmd.extend(["--turns-per-session", str(turns_per_session)])
            if inter_turn_delay_ms > 0:
                cmd.extend(["--inter-turn-delay-ms", str(inter_turn_delay_ms)])
            cmd.extend(shlex.split(env("DYNOSIM_EXTRA_ARGS", "")))

            print("\n=== DynoSim synthetic run ===", flush=True)
            print(f"prefix_ratio={prefix_ratio:g} router_mode={router_mode}", flush=True)
            print(" ".join(shlex.quote(part) for part in cmd), flush=True)
            with stdout_log.open("w") as log:
                process = subprocess.run(cmd, text=True, stdout=log, stderr=subprocess.STDOUT)
            if process.returncode != 0:
                print(stdout_log.read_text(), file=sys.stderr)
                raise subprocess.CalledProcessError(process.returncode, cmd)

            report = load_json(report_json)
            row = dynosim_metrics(report, prefix_ratio, router_mode, report_json, num_workers)
            rows.append(row)
            print(json.dumps(row, sort_keys=True), flush=True)

    return rows


def run_aiperf(
    output_dir: pathlib.Path,
    prefix_ratios: list[float],
    isl: int,
    osl: int,
    request_count: int,
    concurrency: int,
    num_prefix_prompts: int,
) -> list[dict[str, Any]]:
    endpoint_url = env("ENDPOINT_URL", "http://nemotron-omni-vllm-agg-frontend:8000")
    model = env("MODEL_NAME", "nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-NVFP4")
    tokenizer = env("TOKENIZER_NAME", model) or model
    seed = env_int("RANDOM_SEED", 0)
    timeout = env_int("REQUEST_TIMEOUT_SECONDS", 900)
    ui_type = env("AIPERF_UI_TYPE", "none")
    tokenizer_trust_remote_code = env_bool("TOKENIZER_TRUST_REMOTE_CODE", True)
    extra_inputs = build_extra_inputs(osl)
    extra_args = shlex.split(env("AIPERF_EXTRA_ARGS", ""))

    rows: list[dict[str, Any]] = []
    for index, prefix_ratio in enumerate(prefix_ratios):
        prefix_length = int(isl * prefix_ratio)
        synthetic_length = max(1, isl - prefix_length)
        artifact_dir = output_dir / "aiperf" / f"prefix_ratio_{prefix_ratio:g}"
        artifact_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            "aiperf",
            "profile",
            "--model",
            model,
            "--tokenizer",
            tokenizer,
            "--url",
            endpoint_url,
            "--endpoint-type",
            "chat",
            "--streaming",
            "--use-legacy-max-tokens",
            "--prompt-input-tokens-mean",
            str(synthetic_length),
            "--prompt-input-tokens-stddev",
            str(max(1, synthetic_length // 4)),
            "--prompt-output-tokens-mean",
            str(osl),
            "--prompt-output-tokens-stddev",
            str(max(1, osl // 4)),
            "--prompt-prefix-length",
            str(prefix_length),
            "--prompt-prefix-pool-size",
            str(num_prefix_prompts),
            "--request-count",
            str(request_count),
            "--num-dataset-entries",
            str(request_count),
            "--concurrency",
            str(concurrency),
            "--random-seed",
            str(seed + index),
            "--dataset-sampling-strategy",
            "shuffle",
            "--request-timeout-seconds",
            str(timeout),
            "--artifact-dir",
            str(artifact_dir),
            "--header",
            "Authorization: Bearer NOT USED",
            "--header",
            "Accept: text/event-stream",
            "--no-gpu-telemetry",
            "--ui-type",
            ui_type,
        ]

        if extra_inputs:
            cmd.extend(["--extra-inputs", json.dumps(extra_inputs, separators=(",", ":"))])
        if tokenizer_trust_remote_code:
            cmd.append("--tokenizer-trust-remote-code")
        if not env_bool("SERVER_METRICS", True):
            cmd.append("--no-server-metrics")
        cmd.extend(extra_args)

        print("\n=== AIPerf live validation run ===", flush=True)
        print(
            f"prefix_ratio={prefix_ratio:g} prefix_length={prefix_length} "
            f"synthetic_length={synthetic_length}",
            flush=True,
        )
        run_checked(cmd)

        profile_export = find_profile_export(artifact_dir)
        result = load_json(profile_export)
        row = aiperf_metrics(result, prefix_ratio, artifact_dir, profile_export)
        rows.append(row)
        print(json.dumps(row, sort_keys=True), flush=True)

    return rows


def run() -> int:
    prefix_ratios = env_ratios("PREFIX_RATIOS", "0.1,0.5,0.9")
    isl = env_int("INPUT_SEQUENCE_LENGTH", 4096)
    osl = env_int("OUTPUT_SEQUENCE_LENGTH", 128)
    request_count = env_int("REQUEST_COUNT", 200)
    concurrency = env_int("CONCURRENCY", 4)
    num_prefix_prompts = env_int("NUM_PREFIX_PROMPTS", 8)
    output_dir = pathlib.Path(env("OUTPUT_DIR", "/results/dynosim-aiperf-demo"))
    output_dir.mkdir(parents=True, exist_ok=True)

    run_config = {
        "endpoint_url": env("ENDPOINT_URL", "http://nemotron-omni-vllm-agg-frontend:8000"),
        "model": env("MODEL_NAME", "nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-NVFP4"),
        "prefix_ratios": prefix_ratios,
        "input_sequence_length": isl,
        "output_sequence_length": osl,
        "request_count": request_count,
        "concurrency": concurrency,
        "num_prefix_prompts": num_prefix_prompts,
        "run_dynosim": env_bool("RUN_DYNOSIM", True),
        "run_aiperf": env_bool("RUN_AIPERF", True),
        "dynosim_num_workers": env_int("DYNOSIM_NUM_WORKERS", 2),
        "dynosim_router_modes": env_list("DYNOSIM_ROUTER_MODES", "round_robin,kv_router"),
        "dynosim_engine_args": env_json_object(
            "DYNOSIM_ENGINE_ARGS_JSON",
            default_dynosim_engine_args(concurrency),
        ),
        "dynosim_router_config": env_json_object(
            "DYNOSIM_ROUTER_CONFIG_JSON",
            {"router_queue_policy": "fcfs"},
        ),
    }
    (output_dir / "run_config.json").write_text(
        json.dumps(run_config, indent=2, sort_keys=True) + "\n"
    )

    dynosim_rows: list[dict[str, Any]] = []
    if env_bool("RUN_DYNOSIM", True):
        dynosim_rows = run_dynosim(
            output_dir,
            prefix_ratios,
            isl,
            osl,
            request_count,
            concurrency,
            num_prefix_prompts,
        )
        write_rows(output_dir / "dynosim_summary.json", dynosim_rows)
        write_csv(output_dir / "dynosim_summary.csv", dynosim_rows)

    aiperf_rows: list[dict[str, Any]] = []
    if env_bool("RUN_AIPERF", True):
        aiperf_rows = run_aiperf(
            output_dir,
            prefix_ratios,
            isl,
            osl,
            request_count,
            concurrency,
            num_prefix_prompts,
        )
        write_rows(output_dir / "aiperf_summary.json", aiperf_rows)
        write_csv(output_dir / "aiperf_summary.csv", aiperf_rows)

    combined_rows = dynosim_rows + aiperf_rows
    if combined_rows:
        write_rows(output_dir / "combined_summary.json", combined_rows)
        write_csv(output_dir / "combined_summary.csv", combined_rows)
        decision_summary = build_decision_summary(combined_rows)
        if env_bool("GENERATE_DGDR_SPEC", True):
            decision_summary["kubernetes_spec"] = write_kubernetes_spec_artifacts(
                output_dir,
                combined_rows,
                decision_summary,
                isl,
                osl,
                concurrency,
            )
        (output_dir / "decision_summary.json").write_text(
            json.dumps(decision_summary, indent=2, sort_keys=True) + "\n"
        )
        write_csv(output_dir / "decision_summary.csv", decision_summary["prefix_rows"])
        print_decision_summary(decision_summary)

    print("\n=== DynoSim + AIPerf combined summary ===", flush=True)
    print(json.dumps(combined_rows, indent=2, sort_keys=True), flush=True)
    print(f"output_dir={output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except subprocess.CalledProcessError as exc:
        print(f"Command failed with exit code {exc.returncode}", file=sys.stderr)
        raise
