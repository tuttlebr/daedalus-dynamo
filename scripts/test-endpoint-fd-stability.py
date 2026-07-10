#!/usr/bin/env python3
"""Run sustained OpenAI traffic and verify agentgateway FD usage stabilizes."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import ssl
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


EMFILE_MARKERS = ("too many open files", "os error 24", "emfile")
FD_METRICS = (
    "daedalus_gateway_process_open_fds",
    "daedalus_gateway_process_soft_limit_fds",
)


@dataclass
class FDSample:
    timestamp: float
    pod: str
    open_fds: int
    soft_limit: int

    @property
    def utilization(self) -> float:
        return self.open_fds / self.soft_limit if self.soft_limit else 1.0


class ResultCounter:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.statuses: Counter[str] = Counter()
        self.latencies: list[float] = []
        self.emfile_errors = 0

    def record(self, status: str, latency: float, body: bytes = b"") -> None:
        lowered = body.decode("utf-8", "replace").lower()
        with self.lock:
            self.statuses[status] += 1
            self.latencies.append(latency)
            if any(marker in lowered for marker in EMFILE_MARKERS):
                self.emfile_errors += 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run sustained interactive-chat traffic while sampling every gateway "
            "pod's FD exporter through the Kubernetes API."
        )
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("ENDPOINT_URL", "http://192.168.1.234:32359"),
        help="Endpoint base URL or full /v1/chat/completions URL.",
    )
    parser.add_argument("--model", default=os.environ.get("MODEL_ID", ""))
    parser.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY", ""))
    parser.add_argument("--namespace", default="daedalus-dynamo")
    parser.add_argument("--gateway", default="inference-gateway")
    parser.add_argument("--kubectl", default="kubectl")
    parser.add_argument("--duration", type=int, default=300)
    parser.add_argument("--cooldown", type=int, default=90)
    parser.add_argument("--sample-interval", type=float, default=10.0)
    parser.add_argument("--concurrency", type=int, default=64)
    parser.add_argument("--input-tokens", type=int, default=900)
    parser.add_argument("--output-tokens", type=int, default=150)
    parser.add_argument("--request-timeout", type=float, default=300.0)
    parser.add_argument("--max-error-rate", type=float, default=0.05)
    parser.add_argument("--max-fd-utilization", type=float, default=0.80)
    parser.add_argument("--fd-growth-allowance", type=int, default=96)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.concurrency < 1 or args.duration < 1 or args.cooldown < 1:
        parser.error("concurrency, duration, and cooldown must be positive")
    if not 0 <= args.max_error_rate <= 1 or not 0 < args.max_fd_utilization < 1:
        parser.error("rate thresholds must be between zero and one")
    return args


def endpoint_paths(raw_url: str) -> tuple[Any, str, str]:
    parsed = urlsplit(raw_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"invalid endpoint URL: {raw_url}")
    path = parsed.path.rstrip("/")
    if path.endswith("/chat/completions"):
        chat_path = path
        models_path = path[: -len("/chat/completions")] + "/models"
    elif path.endswith("/v1"):
        chat_path = f"{path}/chat/completions"
        models_path = f"{path}/models"
    else:
        prefix = path if path else ""
        chat_path = f"{prefix}/v1/chat/completions"
        models_path = f"{prefix}/v1/models"
    return parsed, chat_path, models_path


def new_connection(parsed: Any, timeout: float) -> http.client.HTTPConnection:
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if parsed.scheme == "https":
        return http.client.HTTPSConnection(
            parsed.hostname, port, timeout=timeout, context=ssl.create_default_context()
        )
    return http.client.HTTPConnection(parsed.hostname, port, timeout=timeout)


def request_headers(api_key: str) -> dict[str, str]:
    headers = {"content-type": "application/json", "connection": "keep-alive"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    return headers


def discover_model(
    parsed: Any, models_path: str, api_key: str, timeout: float
) -> str:
    conn = new_connection(parsed, timeout)
    try:
        conn.request("GET", models_path, headers=request_headers(api_key))
        response = conn.getresponse()
        body = response.read()
        if response.status != 200:
            raise RuntimeError(
                f"model discovery returned HTTP {response.status}: {body[:500].decode('utf-8', 'replace')}"
            )
        data = json.loads(body)
        models = data.get("data", [])
        if not models or not models[0].get("id"):
            raise RuntimeError("model discovery returned no model IDs")
        return str(models[0]["id"])
    finally:
        conn.close()


def interactive_prompt(target_tokens: int) -> str:
    sentence = (
        "Review the deployment behavior, identify the operational risk, and give a concise "
        "engineering recommendation with the evidence that supports it. "
    )
    # This deliberately approximates the suite's 900-token interactive-chat profile.
    target_words = max(1, int(target_tokens * 0.75))
    words = (sentence * ((target_words // len(sentence.split())) + 1)).split()
    return " ".join(words[:target_words])


def load_worker(
    stop_at: float,
    parsed: Any,
    chat_path: str,
    headers: dict[str, str],
    payload: bytes,
    timeout: float,
    results: ResultCounter,
) -> None:
    conn: http.client.HTTPConnection | None = None
    while time.monotonic() < stop_at:
        started = time.monotonic()
        body = b""
        try:
            if conn is None:
                conn = new_connection(parsed, timeout)
            conn.request("POST", chat_path, body=payload, headers=headers)
            response = conn.getresponse()
            body = response.read()
            results.record(str(response.status), time.monotonic() - started, body)
            if response.will_close:
                conn.close()
                conn = None
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            results.record(f"client:{type(exc).__name__}", time.monotonic() - started, str(exc).encode())
            if conn is not None:
                conn.close()
                conn = None
            time.sleep(0.05)
    if conn is not None:
        conn.close()


def kubectl_json(args: argparse.Namespace, command: list[str]) -> Any:
    completed = subprocess.run(
        [args.kubectl, *command],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(completed.stdout)


def gateway_pods(args: argparse.Namespace) -> list[str]:
    data = kubectl_json(
        args,
        [
            "get",
            "pods",
            "-n",
            args.namespace,
            "-l",
            f"gateway.networking.k8s.io/gateway-name={args.gateway}",
            "-o",
            "json",
        ],
    )
    return [
        item["metadata"]["name"]
        for item in data.get("items", [])
        if item.get("status", {}).get("phase") == "Running"
    ]


def parse_prometheus_metrics(raw: str) -> dict[str, float]:
    found: dict[str, float] = {}
    for line in raw.splitlines():
        if not line or line.startswith("#"):
            continue
        name = line.split("{", 1)[0].split(None, 1)[0]
        if name not in FD_METRICS:
            continue
        try:
            found[name] = float(line.rsplit(None, 1)[1])
        except (IndexError, ValueError):
            continue
    return found


def sample_gateway_fds(args: argparse.Namespace) -> list[FDSample]:
    now = time.time()
    samples: list[FDSample] = []
    for pod in gateway_pods(args):
        completed = subprocess.run(
            [
                args.kubectl,
                "get",
                "--raw",
                f"/api/v1/namespaces/{args.namespace}/pods/{pod}:9090/proxy/metrics",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        metrics = parse_prometheus_metrics(completed.stdout)
        if any(name not in metrics for name in FD_METRICS):
            raise RuntimeError(f"FD exporter metrics are incomplete for pod {pod}")
        samples.append(
            FDSample(
                timestamp=now,
                pod=pod,
                open_fds=int(metrics[FD_METRICS[0]]),
                soft_limit=int(metrics[FD_METRICS[1]]),
            )
        )
    if not samples:
        raise RuntimeError("no running gateway pods were found")
    return samples


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


def main() -> int:
    args = parse_args()
    parsed, chat_path, models_path = endpoint_paths(args.url)
    model = args.model or discover_model(parsed, models_path, args.api_key, args.request_timeout)
    payload = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": interactive_prompt(args.input_tokens)}],
            "max_tokens": args.output_tokens,
            "temperature": 0,
            "stream": False,
        },
        separators=(",", ":"),
    ).encode()

    samples: list[FDSample] = []
    telemetry_errors: list[str] = []
    baseline = sample_gateway_fds(args)
    samples.extend(baseline)
    print(
        f"model={model} concurrency={args.concurrency} duration={args.duration}s "
        f"baseline=" + ",".join(f"{s.pod}:{s.open_fds}/{s.soft_limit}" for s in baseline),
        flush=True,
    )

    results = ResultCounter()
    stop_at = time.monotonic() + args.duration
    workers = [
        threading.Thread(
            target=load_worker,
            args=(
                stop_at,
                parsed,
                chat_path,
                request_headers(args.api_key),
                payload,
                args.request_timeout,
                results,
            ),
            daemon=True,
        )
        for _ in range(args.concurrency)
    ]
    for worker in workers:
        worker.start()

    while any(worker.is_alive() for worker in workers):
        time.sleep(min(args.sample_interval, max(0.1, stop_at - time.monotonic())))
        try:
            current = sample_gateway_fds(args)
            samples.extend(current)
            print(
                "fd_sample="
                + ",".join(f"{s.pod}:{s.open_fds}/{s.soft_limit}" for s in current),
                flush=True,
            )
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            telemetry_errors.append(str(exc))
    for worker in workers:
        worker.join()

    cooldown_end = time.monotonic() + args.cooldown
    while time.monotonic() < cooldown_end:
        time.sleep(max(0.05, min(args.sample_interval, cooldown_end - time.monotonic())))
        try:
            samples.extend(sample_gateway_fds(args))
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            telemetry_errors.append(str(exc))

    final = sample_gateway_fds(args)
    samples.extend(final)
    grouped: dict[str, list[FDSample]] = defaultdict(list)
    for sample in samples:
        grouped[sample.pod].append(sample)

    total = sum(results.statuses.values())
    errors = sum(count for status, count in results.statuses.items() if not status.startswith("2"))
    error_rate = errors / total if total else 1.0
    max_utilization = max((sample.utilization for sample in samples), default=1.0)
    stabilization_failures: list[str] = []
    current_pods = {sample.pod for sample in final}
    for pod in current_pods:
        observations = grouped[pod]
        first, last = observations[0], observations[-1]
        if last.open_fds > first.open_fds + args.fd_growth_allowance:
            stabilization_failures.append(
                f"{pod} finished at {last.open_fds} FDs vs first {first.open_fds} "
                f"(allowance {args.fd_growth_allowance})"
            )

    failures: list[str] = []
    if total == 0:
        failures.append("no requests completed")
    if results.emfile_errors:
        failures.append(f"observed {results.emfile_errors} EMFILE responses")
    if error_rate > args.max_error_rate:
        failures.append(
            f"error rate {error_rate:.3%} exceeded {args.max_error_rate:.3%}"
        )
    if max_utilization > args.max_fd_utilization:
        failures.append(
            f"peak FD utilization {max_utilization:.3%} exceeded {args.max_fd_utilization:.3%}"
        )
    failures.extend(stabilization_failures)
    if telemetry_errors:
        failures.append(f"FD telemetry had {len(telemetry_errors)} sampling error(s)")

    report = {
        "passed": not failures,
        "endpoint": args.url,
        "model": model,
        "concurrency": args.concurrency,
        "duration_seconds": args.duration,
        "cooldown_seconds": args.cooldown,
        "requests": total,
        "statuses": dict(results.statuses),
        "error_rate": error_rate,
        "emfile_errors": results.emfile_errors,
        "latency_seconds": {
            "p50": percentile(results.latencies, 0.50),
            "p95": percentile(results.latencies, 0.95),
            "p99": percentile(results.latencies, 0.99),
        },
        "max_fd_utilization": max_utilization,
        "baseline": [asdict(sample) for sample in baseline],
        "final": [asdict(sample) for sample in final],
        "telemetry_errors": telemetry_errors,
        "failures": failures,
    }
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n")
    return 0 if not failures else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
