#!/usr/bin/env python3

import csv
import json
import os
import pathlib
import shlex
import subprocess
import sys
from typing import Any


def env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def env_int(name: str, default: int) -> int:
    value = env(name, str(default))
    try:
        return int(value)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {value!r}") from exc


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


def env_json_object(name: str) -> dict[str, Any]:
    value = env(name, "")
    if not value:
        return {}
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


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def env_ratios(name: str, default: str) -> list[float]:
    raw = env(name, default).replace(",", " ")
    ratios = []
    for item in raw.split():
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
        if base_cache_control is not None and not isinstance(
            base_cache_control, dict
        ):
            raise SystemExit("nvext.cache_control must be an object")
        nvext["cache_control"] = deep_merge(base_cache_control or {}, cache_control)

    nvext = deep_merge(nvext, env_json_object("DYNAMO_NVEXT_JSON"))
    if nvext:
        extra_inputs["nvext"] = nvext

    return deep_merge(extra_inputs, env_json_object("DYNAMO_EXTRA_INPUTS_JSON"))


def run() -> int:
    endpoint_url = env("ENDPOINT_URL", "http://gemma-4-31b-it-vllm-agg-0:8000")
    model = env("MODEL_NAME", "google/gemma-4-31B-it")
    tokenizer = env("TOKENIZER_NAME", model) or model
    prefix_ratios = env_ratios("PREFIX_RATIOS", "0.1,0.5,0.9")
    isl = env_int("INPUT_SEQUENCE_LENGTH", 4096)
    osl = env_int("OUTPUT_SEQUENCE_LENGTH", 128)
    request_count = env_int("REQUEST_COUNT", 40)
    concurrency = env_int("CONCURRENCY", 4)
    num_prefix_prompts = env_int("NUM_PREFIX_PROMPTS", 8)
    seed = env_int("RANDOM_SEED", 0)
    timeout = env_int("REQUEST_TIMEOUT_SECONDS", 900)
    ui_type = env("AIPERF_UI_TYPE", "none")
    tokenizer_trust_remote_code = env_bool("TOKENIZER_TRUST_REMOTE_CODE", True)
    patch_dynamo_stream_events = env_bool("AIPERF_PATCH_DYNAMO_STREAM_EVENTS", True)
    output_dir = pathlib.Path(env("OUTPUT_DIR", "/results/aiperf-router-demo"))
    output_dir.mkdir(parents=True, exist_ok=True)
    extra_inputs = build_extra_inputs(osl)

    config_path = output_dir / "run_config.json"
    config_path.write_text(
        json.dumps(
            {
                "endpoint_url": endpoint_url,
                "model": model,
                "tokenizer": tokenizer,
                "prefix_ratios": prefix_ratios,
                "input_sequence_length": isl,
                "output_sequence_length": osl,
                "request_count": request_count,
                "concurrency": concurrency,
                "num_prefix_prompts": num_prefix_prompts,
                "random_seed": seed,
                "tokenizer_trust_remote_code": tokenizer_trust_remote_code,
                "aiperf_patch_dynamo_stream_events": patch_dynamo_stream_events,
                "dynamo_agent_hints_dynamic_osl": env_bool(
                    "DYNAMO_AGENT_HINTS_DYNAMIC_OSL", True
                ),
                "extra_inputs": extra_inputs,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )

    rows: list[dict[str, Any]] = []
    extra_args = shlex.split(env("AIPERF_EXTRA_ARGS", ""))

    for index, prefix_ratio in enumerate(prefix_ratios):
        prefix_length = int(isl * prefix_ratio)
        synthetic_length = max(1, isl - prefix_length)
        artifact_dir = output_dir / f"prefix_ratio_{prefix_ratio:g}"
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
            cmd.extend(
                [
                    "--extra-inputs",
                    json.dumps(extra_inputs, separators=(",", ":")),
                ]
            )
        if tokenizer_trust_remote_code:
            cmd.append("--tokenizer-trust-remote-code")
        if not env_bool("SERVER_METRICS", True):
            cmd.append("--no-server-metrics")
        cmd.extend(extra_args)

        print("\n=== AIPerf prefix-ratio run ===", flush=True)
        print(f"prefix_ratio={prefix_ratio:g} prefix_length={prefix_length} synthetic_length={synthetic_length}", flush=True)
        print(" ".join(shlex.quote(part) for part in cmd), flush=True)
        subprocess.run(cmd, check=True)

        result_path = find_profile_export(artifact_dir)
        result = load_json(result_path)
        row = {
            "prefix_ratio": prefix_ratio,
            "prefix_length": prefix_length,
            "synthetic_input_length": synthetic_length,
            "artifact_dir": str(artifact_dir),
            "profile_export": str(result_path) if result_path else "",
            "ttft_p50_ms": metric_value(result, "time_to_first_token", "p50"),
            "ttft_p90_ms": metric_value(result, "time_to_first_token", "p90"),
            "itl_p50_ms": metric_value(result, "inter_token_latency", "p50"),
            "itl_p90_ms": metric_value(result, "inter_token_latency", "p90"),
            "request_latency_p50_ms": metric_value(result, "request_latency", "p50"),
            "request_latency_p90_ms": metric_value(result, "request_latency", "p90"),
        }
        rows.append(row)
        print(json.dumps(row, sort_keys=True), flush=True)

    summary_json = output_dir / "summary.json"
    summary_csv = output_dir / "summary.csv"
    summary_json.write_text(json.dumps(rows, indent=2, sort_keys=True) + "\n")
    with summary_csv.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    print("\n=== AIPerf summary ===", flush=True)
    print(summary_json.read_text(), flush=True)
    print(f"summary_csv={summary_csv}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except subprocess.CalledProcessError as exc:
        print(f"AIPerf failed with exit code {exc.returncode}", file=sys.stderr)
        raise
