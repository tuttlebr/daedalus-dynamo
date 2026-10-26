"""Exercise the dashboard's actual PromQL with synthetic Prometheus series.

Set PROMTOOL to a local promtool executable to run the semantic checks.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "observability/grafana/build-efficiency-dashboard.py"
SPEC = importlib.util.spec_from_file_location("efficiency_dashboard", SOURCE)
assert SPEC and SPEC.loader
dashboard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dashboard)
PROMTOOL = os.environ.get("PROMTOOL") or shutil.which("promtool")


def query(expression: str, *, active: bool = True, deployment: str = ".+") -> str:
    for name, value in {
        "${deployment:regex}": deployment, "$__range": "10m", "$trend_window": "5m",
        "$active_only": "1" if active else "0",
        "$ttft_objective_ms": "1000", "$itl_objective_ms": "100",
    }.items():
        expression = expression.replace(name, value)
    return expression


def series(metric: str, values: str, **labels: str) -> dict:
    encoded = ",".join(f"{key}={json.dumps(value)}" for key, value in labels.items())
    return {"series": f"{metric}{{{encoded}}}", "values": values}


def pod(name: str, deployment: str = "alpha", role: str = "frontend", values: str = "1+0x40") -> dict:
    return series("kube_pod_labels", values, namespace="daedalus-dynamo", pod=name,
                  label_nvidia_com_dynamo_graph_deployment_name=deployment,
                  label_nvidia_com_dynamo_component_type=role)


def expected(expression: str, value: float | None, deployment: str = "alpha", active: bool = True) -> dict:
    expression = query(expression, active=active, deployment=deployment)
    if value is not None:
        expression = f"abs(({expression}) - ({value})) < bool 0.00000001"
    return {"expr": expression, "eval_time": "10m",
            "exp_samples": [] if value is None else [
                {"labels": f'{{deployment="{deployment}"}}', "value": 1}
            ]}


class DashboardArtifactTests(unittest.TestCase):
    def test_import_artifact_matches_generator(self) -> None:
        artifact = SOURCE.with_name("dynamo-efficiency-dashboard.json")
        self.assertEqual(json.loads(artifact.read_text()), dashboard.build_dashboard())


@unittest.skipUnless(PROMTOOL, "set PROMTOOL to run synthetic PromQL tests")
class DashboardPromQLTests(unittest.TestCase):
    def run_fixture(self, inputs: list[dict], checks: list[dict]) -> None:
        document = {"rule_files": [], "evaluation_interval": "15s", "tests": [{
            "interval": "15s", "input_series": inputs, "promql_expr_test": checks,
        }]}
        with tempfile.TemporaryDirectory(prefix="dynamo-efficiency-test-") as directory:
            fixture = Path(directory) / "queries.yaml"
            fixture.write_text(yaml.safe_dump(document, sort_keys=False))
            completed = subprocess.run([str(PROMTOOL), "test", "rules", str(fixture)],
                                       capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_histograms_weight_requests_deduplicate_scrapes_and_pool_buckets(self) -> None:
        inputs = [pod("frontend-a"), pod("frontend-b"), pod("frontend-c", "beta")]
        metric = "dynamo_frontend_time_to_first_token_seconds"
        # A: 1 request/s at 100 ms; B: 9 requests/s at 500 ms. Weighted mean = 460 ms.
        # A's duplicate scrape must not give its requests extra weight.
        for name, job, count, total in [("frontend-a", "one", 15, 1.5),
                                        ("frontend-a", "duplicate", 15, 1.5),
                                        ("frontend-b", "one", 135, 67.5),
                                        ("frontend-c", "one", 15, 15)]:
            labels = {"namespace": "daedalus-dynamo", "pod": name, "model": "model", "job": job}
            inputs.extend([series(metric + "_count", f"0+{count}x40", **labels),
                           series(metric + "_sum", f"0+{total}x40", **labels)])
            for le, increment in [("0.1", count if name == "frontend-a" else 0),
                                  ("0.5", count if name != "frontend-c" else 0),
                                  ("1", count), ("+Inf", count)]:
                inputs.append(series(metric + "_bucket", f"0+{increment}x40", le=le, **labels))
        avg, p95 = dashboard.histogram(metric, "$__range", 1000)
        self.run_fixture(inputs, [expected(avg, 460), expected(p95, 477.7777777777778),
                                  expected(avg, 1000, "beta"), expected(p95, 975, "beta")])

    def test_histogram_counter_resets_are_handled_before_aggregation(self) -> None:
        metric = "dynamo_frontend_input_sequence_tokens"
        labels = {"namespace": "daedalus-dynamo", "pod": "frontend", "model": "model"}
        inputs = [pod("frontend"),
                  series(metric + "_count", "0+1x19 0+1x20", **labels),
                  series(metric + "_sum", "0+200x19 0+200x20", **labels)]
        avg, _ = dashboard.histogram(metric, "$__range")
        self.run_fixture(inputs, [expected(avg, 200)])

    def test_idle_and_missing_histograms_remain_distinct(self) -> None:
        metric = "dynamo_frontend_time_to_first_token_seconds"
        labels = {"namespace": "daedalus-dynamo", "pod": "frontend", "model": "model"}
        avg, p95 = dashboard.histogram(metric, "$__range", 1000, idle=True)
        inputs = [pod("frontend"), series(metric + "_count", "12+0x40", **labels)]
        self.run_fixture(inputs, [expected(avg, -1), expected(p95, -1)])
        self.run_fixture([pod("frontend")], [expected(avg, None), expected(p95, None)])

    def test_selected_range_keeps_replaced_pods(self) -> None:
        metric = "dynamo_frontend_output_sequence_tokens"
        labels = {"namespace": "daedalus-dynamo", "pod": "old-frontend", "model": "model"}
        inputs = [pod("old-frontend", values="1+0x19 stale _x20"),
                  series(metric + "_count", "0+1x19 stale _x20", **labels),
                  series(metric + "_sum", "0+300x19 stale _x20", **labels)]
        avg, _ = dashboard.histogram(metric, "$__range")
        self.run_fixture(inputs, [expected(avg, 300)])

    def test_prefix_reuse_weights_tokens_and_deduplicates_engine_scrapes(self) -> None:
        inputs = [pod("worker-a", role="decode"), pod("worker-b", "beta", role="decode")]
        # Alpha has 12/15 hits on engine 0 and 27/135 on engine 1: pooled = 26%.
        for name, engine, job, hits, queries in [
            ("worker-a", "0", "one", 12, 15),
            ("worker-a", "0", "duplicate", 12, 15),
            ("worker-a", "1", "one", 27, 135),
            ("worker-b", "0", "one", 15, 15),
        ]:
            labels = {"namespace": "daedalus-dynamo", "pod": name,
                      "model_name": "model", "engine": engine, "job": job}
            inputs.extend([
                series("vllm:prefix_cache_hits_total", f"0+{hits}x40", **labels),
                series("vllm:prefix_cache_queries_total", f"0+{queries}x40", **labels),
            ])
        summary = dashboard.prefix_reuse("$__range")
        trend = dashboard.prefix_reuse("$trend_window")
        self.run_fixture(inputs, [expected(summary, 0.26), expected(trend, 0.26),
                                  expected(summary, 1, "beta"),
                                  expected(summary, 0.26, active=False)])

    def test_prefix_reuse_preserves_zero_hits_and_suppresses_missing_or_idle_counters(self) -> None:
        labels = {"namespace": "daedalus-dynamo", "pod": "worker", "engine": "0"}
        worker = pod("worker", role="decode")
        hits = series("vllm:prefix_cache_hits_total", "0+0x40", **labels)
        queries = series("vllm:prefix_cache_queries_total", "0+10x40", **labels)
        expression = dashboard.prefix_reuse("$__range")
        self.run_fixture([worker, hits, queries], [expected(expression, 0)])
        for counters in [[], [hits], [queries],
                         [hits, series("vllm:prefix_cache_queries_total", "10+0x40", **labels)]]:
            with self.subTest(counters=counters):
                self.run_fixture([worker, *counters], [expected(expression, None)])

    def test_prefix_reuse_requires_matching_counters_for_every_observed_engine(self) -> None:
        labels = {"namespace": "daedalus-dynamo", "pod": "worker", "engine": "0"}
        expression = dashboard.prefix_reuse("$__range")
        complete = [pod("worker", role="decode"),
                    series("vllm:prefix_cache_hits_total", "0+7x40", **labels),
                    series("vllm:prefix_cache_queries_total", "0+10x40", **labels)]
        for missing_metric in ["vllm:prefix_cache_hits_total", "vllm:prefix_cache_queries_total"]:
            with self.subTest(missing_metric=missing_metric):
                incomplete = series(missing_metric, "0+5x40", **{**labels, "engine": "1"})
                self.run_fixture([*complete, incomplete], [expected(expression, None)])

    def test_prefix_reuse_handles_resets_and_replaced_workers(self) -> None:
        expression = dashboard.prefix_reuse("$__range")
        labels = {"namespace": "daedalus-dynamo", "pod": "worker", "engine": "0"}
        self.run_fixture([
            pod("worker", role="decode"),
            series("vllm:prefix_cache_hits_total", "0+7x19 0+7x20", **labels),
            series("vllm:prefix_cache_queries_total", "0+10x19 0+10x20", **labels),
        ], [expected(expression, 0.7)])
        # The old worker's contribution survives its stale pod-label series.
        self.run_fixture([
            pod("old-worker", role="decode", values="1+0x19 stale _x20"),
            series("vllm:prefix_cache_hits_total", "0+7x19 stale _x20",
                   **{**labels, "pod": "old-worker"}),
            series("vllm:prefix_cache_queries_total", "0+10x19 stale _x20",
                   **{**labels, "pod": "old-worker"}),
        ], [expected(expression, 0.7)])

    def workers(self, *, missing_power: bool = False, shared: bool = False, mig: bool = False) -> list[dict]:
        inputs = [pod("frontend"), pod("worker", role="decode"),
                  series("kube_pod_status_phase", "1+0x40", namespace="daedalus-dynamo", pod="worker", phase="Running"),
                  series("kube_pod_container_resource_requests", "1+0x40", namespace="daedalus-dynamo",
                         pod="worker", container="main", resource="nvidia_com_gpu"),
                  # Output is produced only in minutes 0..5, followed by idle time.
                  series("dynamo_frontend_output_tokens_total", "0+150x20 3000+0x19",
                         namespace="daedalus-dynamo", pod="frontend", model="model")]
        for job in ["one", "duplicate"]:
            inputs.append(series("dynamo_component_gpu_cache_usage_percent", "0.8+0x20 0+0x19",
                                 namespace="daedalus-dynamo", pod="worker", dp_rank="0", job=job))
        labels = {"Hostname": "gpu-host", "UUID": "GPU-one", "exported_namespace": "daedalus-dynamo",
                  "exported_pod": "worker", "instance": "dcgm:9400"}
        inputs.append(series("DCGM_FI_DEV_GPU_UTIL", "0+0x40", **labels))
        if not missing_power:
            inputs.append(series("DCGM_FI_DEV_POWER_USAGE", "100+0x40", **labels))
        if shared:
            labels.update(exported_namespace="daedalus", exported_pod="another-workload")
            inputs.append(series("DCGM_FI_DEV_GPU_UTIL", "0+0x40", **labels))
        if mig:
            inputs.append(series("kube_pod_container_resource_requests", "1+0x40", namespace="daedalus-dynamo",
                                 pod="worker", container="main", resource="nvidia_com_mig_1g_5gb"))
        return inputs

    def test_active_sampling_does_not_dilute_with_idle_zeros(self) -> None:
        kv, efficiency, _ = dashboard.samples()
        kv_avg, kv_p95 = dashboard.sampled_summary(kv, "$__range")
        efficiency_avg, efficiency_p95 = dashboard.sampled_summary(efficiency, "$__range")
        # Five minute-aligned active samples; minute 0 lacks two rate samples.
        # Idle minutes are included only when explicitly selecting All time.
        self.run_fixture(self.workers(), [
            expected(kv_avg, 0.8), expected(kv_p95, 0.8),
            expected(efficiency_avg, 0.1),
            expected(efficiency_p95, 0.1),
            expected(kv_avg, 0.4, active=False),
            expected(efficiency_avg, 0.05, active=False),
        ])

    def test_efficiency_is_unavailable_with_incomplete_or_shared_power(self) -> None:
        _, efficiency, _ = dashboard.samples()
        average, p95 = dashboard.sampled_summary(efficiency, "$__range")
        for kwargs in [{"missing_power": True}, {"shared": True}, {"mig": True}]:
            with self.subTest(**kwargs):
                self.run_fixture(self.workers(**kwargs), [expected(average, None), expected(p95, None)])
        inputs = self.workers()
        inputs.extend([pod("second-worker", role="prefill"),
                       series("kube_pod_status_phase", "1+0x40", namespace="daedalus-dynamo",
                              pod="second-worker", phase="Running")])
        self.run_fixture(inputs, [expected(average, None), expected(p95, None)])


if __name__ == "__main__":
    unittest.main()
