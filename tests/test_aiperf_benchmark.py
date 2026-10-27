from __future__ import annotations

import json
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_DIR = ROOT / "benchmarks" / "aiperf"
SCRIPT = BENCHMARK_DIR / "run-model-benchmarks.sh"
KUBERNETES_RUNNER = BENCHMARK_DIR / "run-kubernetes-job.sh"
TRACE_SPEC = importlib.util.spec_from_file_location(
    "prepare_agentic_trace", BENCHMARK_DIR / "prepare-agentic-trace.py"
)
assert TRACE_SPEC and TRACE_SPEC.loader
TRACE_MODULE = importlib.util.module_from_spec(TRACE_SPEC)
TRACE_SPEC.loader.exec_module(TRACE_MODULE)


class AIPerfBenchmarkTests(unittest.TestCase):
    def render_kubernetes_job(
        self, *variables: str, cluster_ip: str = "10.111.176.80", lookup_fails: bool = False
    ) -> tuple[subprocess.CompletedProcess[str], str]:
        with tempfile.TemporaryDirectory() as directory:
            fake_kubectl = Path(directory) / "kubectl"
            call_log = Path(directory) / "calls.log"
            fake_kubectl.write_text(
                '#!/usr/bin/env bash\n'
                'printf "%s\\n" "$*" >>"${FAKE_KUBECTL_LOG}"\n'
                'printf "%s" "${FAKE_CLUSTER_IP}"\n'
                'exit "${FAKE_LOOKUP_EXIT_CODE}"\n'
            )
            fake_kubectl.chmod(0o755)
            environment = os.environ.copy()
            environment.update({
                "FAKE_KUBECTL_LOG": str(call_log),
                "FAKE_CLUSTER_IP": cluster_ip,
                "FAKE_LOOKUP_EXIT_CODE": "1" if lookup_fails else "0",
            })
            result = subprocess.run(
                [
                    "make", "--no-print-directory", "benchmark-agentic-render",
                    "AIPERF_RUN_ID=unit-k8s-render",
                    "AIPERF_IMAGE=registry.example.com/aiperf:0.13.0",
                    f"KUBECTL={fake_kubectl}",
                    *variables,
                ],
                cwd=ROOT, env=environment, capture_output=True, text=True,
            )
            return result, call_log.read_text() if call_log.exists() else ""

    def test_discovery_auth_and_generation_key_redaction_without_traffic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake_curl = Path(directory) / "curl"
            fake_curl.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "assert 'Authorization: Bearer ' + os.environ['OPENAI_API_KEY'] in sys.argv\n"
                "assert sys.argv[-1] == 'https://daedalus.ddns.me:32359/v1/models'\n"
                "print(json.dumps({'data': [{'id': 'example/model'}]}))\n"
                "print('200')\n"
            )
            fake_curl.chmod(0o755)
            env = os.environ.copy()
            env.update(
                {
                    "PATH": directory + os.pathsep + env["PATH"],
                    "ENDPOINT_URL": "https://daedalus.ddns.me:32359",
                    "OPENAI_API_KEY": "unit-private-key",
                    "MODELS": "",
                    "ARTIFACTS_DIR": directory,
                    "RUN_ID": "auth-dry-run",
                }
            )
            result = subprocess.run(
                [str(SCRIPT), "--dry-run"], cwd=ROOT, env=env,
                check=True, capture_output=True, text=True,
            )
            self.assertIn("--api-key", result.stdout)
            self.assertIn("REDACTED", result.stdout)
            self.assertNotIn("unit-private-key", result.stdout + result.stderr)

    def test_discovery_explains_rejected_credentials_without_exposing_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake_curl = Path(directory) / "curl"
            fake_curl.write_text('#!/usr/bin/env bash\nprintf "\\n401"\nexit 22\n')
            fake_curl.chmod(0o755)
            environment = os.environ.copy()
            environment.update({
                "PATH": directory + os.pathsep + environment["PATH"],
                "OPENAI_API_KEY": "unit-rejected-key",
                "MODELS": "",
            })
            result = subprocess.run(
                [str(SCRIPT), "--list-models"], cwd=ROOT, env=environment,
                capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("gateway authentication rejected (HTTP 401)", result.stderr)
            self.assertIn("make benchmark-agentic", result.stderr)
            self.assertNotIn("unit-rejected-key", result.stdout + result.stderr)

    def test_dry_run_covers_every_selected_model_and_redacts_key(self) -> None:
        env = os.environ.copy()
        env.update(
            {
                "MODELS": "example/model-a,example/model-b",
                "OPENAI_API_KEY": "test-key",
                "ARTIFACTS_DIR": "/tmp/aiperf-dry-run",
                "RUN_ID": "unit-test",
                "TOKENIZER": "auto",
            }
        )
        result = subprocess.run(
            [str(SCRIPT), "--dry-run"],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertIn("=== example/model-a ===", result.stdout)
        self.assertIn("=== example/model-b ===", result.stdout)
        self.assertEqual(result.stdout.count("aiperf profile"), 2)
        self.assertIn("--tokenizer example/model-a", result.stdout)
        self.assertIn("--tokenizer example/model-b", result.stdout)
        self.assertIn("X-Dynamo-Session-ID", result.stdout)
        self.assertNotIn("--prompt-input-tokens-block-size", result.stdout)
        self.assertIn("--fixed-schedule", result.stdout)
        self.assertNotIn("--request-count", result.stdout)
        self.assertIn("REDACTED", result.stdout)
        self.assertNotIn("test-key", result.stdout)

    def test_container_pins_the_verified_aiperf_release(self) -> None:
        pyproject = (BENCHMARK_DIR / "pyproject.toml").read_text()
        dockerfile = (BENCHMARK_DIR / "Dockerfile").read_text()
        script = SCRIPT.read_text()

        self.assertIn('"aiperf==0.13.0"', pyproject)
        self.assertIn('org.opencontainers.image.version="0.13.0"', dockerfile)
        self.assertIn('EXPECTED_AIPERF_VERSION="0.13.0"', script)
        self.assertIn('MOONCAKE_BLOCK_SIZE="512"', script)
        self.assertIn(
            'AIPERF_SOURCE_COMMIT="794f8bb75f8582f22e412d7e650fc71ca2a3d21a"',
            script,
        )
        self.assertIn("output_length_evidence", script)

    def test_default_concurrency_matches_three_actor_sla(self) -> None:
        script = SCRIPT.read_text()
        compose = (BENCHMARK_DIR / "docker-compose.yaml").read_text()

        self.assertIn('CONCURRENCY="${CONCURRENCY:-3}"', script)
        self.assertIn('CONCURRENCY: "${CONCURRENCY:-3}"', compose)

    def test_default_agentic_shape_targets_seventy_percent_reuse(self) -> None:
        config = json.loads((BENCHMARK_DIR / "agentic-code-config.json").read_text())

        self.assertEqual(config["turns"]["mean"], 5)
        self.assertEqual(config["turns"]["min"], 5)
        self.assertEqual(config["turns"]["max"], 5)
        self.assertEqual(config["block_size"], 512)
        self.assertEqual(config["cache"]["layer1_tokens"], 1024)
        self.assertEqual(config["cache"]["layer1_5_tokens"], 512)
        turns = config["turns"]["mean"]
        shared = config["cache"]["layer1_tokens"] + config["cache"]["layer1_5_tokens"]
        initial = shared + config["cache"]["layer2"]["mean"]
        fresh = config["new_tokens_per_turn"]["mean"]
        output = config["generation_length"]["mean"]
        prompts = [initial + turn * (fresh + output) for turn in range(turns)]
        reusable = sum(prompts) - (initial + (turns - 1) * fresh - shared)
        self.assertAlmostEqual(100 * reusable / sum(prompts), 70, delta=1)
        self.assertLess(max(prompts) + output, config["max_prompt_tokens"])

    def test_trace_spans_duration_preserves_turns_and_counts_cold_prefixes(self) -> None:
        rows = [
            {"session_id": session, "input_length": 512, "output_length": 128,
             "hash_ids": hashes, **timing}
            for session, hashes, timing in (
                ("a", [0], {"timestamp": 0}),
                ("a", [1], {"delay": 250}),
                ("b", [0], {"timestamp": 0}),
                ("b", [2], {"delay": 1000}),
            )
        ]
        scheduled, report = TRACE_MODULE.prepare_trace(rows, 1800)
        self.assertEqual([row["timestamp"] for row in scheduled if "timestamp" in row], [0, 1800000])
        self.assertEqual([row["delay"] for row in scheduled if "delay" in row], [250, 1000])
        self.assertEqual(rows[2]["timestamp"], 0)
        self.assertEqual(report["requests"], 4)
        self.assertEqual(report["total_prompt_tokens"], 3328)
        self.assertEqual(report["estimated_reusable_prompt_tokens"], 1792)
        self.assertAlmostEqual(report["estimated_prefix_reuse_pct"], 100 * 1792 / 3328)
        with self.assertRaisesRegex(ValueError, "at least two"):
            TRACE_MODULE.prepare_trace(rows[:2], 1800)
        self.assertEqual(TRACE_MODULE.prepare_trace(rows[:2], 0)[1]["requests"], 2)

    def test_duration_cannot_be_cut_short_by_timeouts_or_a_single_session(self) -> None:
        for variable in ("AIPERF_MODEL_TIMEOUT_SECONDS=900", "AIPERF_ACTIVE_DEADLINE_SECONDS=1800", "AIPERF_DATASET_SESSIONS=1"):
            with self.subTest(variable=variable):
                result, _ = self.render_kubernetes_job(variable)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("AIPERF_BENCHMARK_DURATION_SECONDS", result.stderr)

    def test_runner_rejects_successful_exit_with_insufficient_measured_duration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake_aiperf = Path(directory) / "aiperf"
            fake_aiperf.write_text(textwrap.dedent('''\
                #!/usr/bin/env python3
                import json, os, sys
                from pathlib import Path
                if sys.argv[1] == '--version':
                    print('0.13.0')
                elif sys.argv[1] == 'synthesize':
                    output = Path(sys.argv[sys.argv.index('--output') + 1])
                    output.mkdir(parents=True, exist_ok=True)
                    rows = [{'session_id': str(i), 'input_length': 512,
                             'output_length': 128, 'hash_ids': [i], 'timestamp': 0}
                            for i in range(2)]
                    (output / 'dataset.jsonl').write_text(
                        ''.join(json.dumps(row) + '\\n' for row in rows))
                elif sys.argv[1] == 'profile':
                    output = Path(sys.argv[sys.argv.index('--artifact-dir') + 1])
                    output.mkdir(parents=True)
                    (output / 'profile_export_aiperf.json').write_text(json.dumps({
                        'benchmark_duration': {'unit': 'sec', 'avg': float(os.environ['FAKE_DURATION'])},
                        'request_count': {'avg': 2},
                        'osl_mismatch_count': {'avg': 0},
                        'osl_mismatch_diff_pct': {'avg': 0},
                    }))
                else:
                    raise SystemExit(2)
                '''))
            fake_aiperf.chmod(0o755)
            for observed, expected_exit in ((1, 1), (11, 0)):
                with self.subTest(observed=observed):
                    environment = os.environ.copy()
                    environment.update({
                        "AIPERF_BIN": str(fake_aiperf), "MODELS": "example/model",
                        "OPENAI_API_KEY": "", "GPU_TELEMETRY": "",
                        "ARTIFACTS_DIR": directory, "RUN_ID": f"duration-{observed}",
                        "DATASET_SESSIONS": "2", "TURNS": "1",
                        "BENCHMARK_DURATION_SECONDS": "10", "MODEL_TIMEOUT_SECONDS": "30",
                        "FAKE_DURATION": str(observed),
                    })
                    result = subprocess.run(
                        [str(SCRIPT)], cwd=ROOT, env=environment,
                        capture_output=True, text=True,
                    )
                    self.assertEqual(result.returncode, expected_exit, result.stdout + result.stderr)
                    summary = json.loads((Path(directory) / f"duration-{observed}" / "run-summary.json").read_text())
                    self.assertEqual(summary["failed_models"], expected_exit)

    def test_make_renders_bounded_all_model_kubernetes_job(self) -> None:
        result, calls = self.render_kubernetes_job()
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(result.stdout)

        self.assertEqual(manifest["kind"], "Job")
        self.assertEqual(manifest["metadata"]["namespace"], "daedalus-dynamo")
        self.assertEqual(manifest["spec"]["backoffLimit"], 0)
        self.assertEqual(manifest["spec"]["activeDeadlineSeconds"], 14400)

        pod_spec = manifest["spec"]["template"]["spec"]
        self.assertEqual(
            pod_spec["containers"][0]["command"],
            ["/opt/aiperf/run-kubernetes-container.sh"],
        )
        self.assertFalse(pod_spec["automountServiceAccountToken"])
        self.assertEqual(pod_spec["restartPolicy"], "Never")
        self.assertTrue(
            pod_spec["containers"][0]["securityContext"]["readOnlyRootFilesystem"]
        )

        environment = {
            item["name"]: item for item in pod_spec["containers"][0]["env"]
        }
        self.assertEqual(
            environment["ENDPOINT_URL"]["value"],
            "https://daedalus.ddns.me:32359",
        )
        self.assertEqual(
            pod_spec["hostAliases"],
            [{"ip": "10.111.176.80", "hostnames": ["daedalus.ddns.me"]}],
        )
        self.assertIn(
            "--context kubernetes-admin@kubernetes get service inference-gateway "
            "-n daedalus-dynamo -o jsonpath={.spec.clusterIP}",
            calls,
        )
        self.assertEqual(environment["CONCURRENCY"]["value"], "3")
        self.assertEqual(environment["TURNS"]["value"], "5")
        self.assertEqual(environment["DATASET_SESSIONS"]["value"], "128")
        self.assertEqual(environment["BENCHMARK_DURATION_SECONDS"]["value"], "1800")
        self.assertGreater(int(environment["MODEL_TIMEOUT_SECONDS"]["value"]), 1800)
        self.assertEqual(environment["RUN_ID"]["value"], "unit-k8s-render")
        self.assertNotIn("MODELS", environment)
        self.assertEqual(
            environment["OPENAI_API_KEY"]["valueFrom"]["secretKeyRef"],
            {"name": "inference-gateway-api-keys", "key": "switchyard"},
        )

    def test_cluster_ip_lookup_failure_never_falls_back_to_public_dns(self) -> None:
        for cluster_ip, lookup_fails in (("", True), ("None", False)):
            with self.subTest(cluster_ip=cluster_ip, lookup_fails=lookup_fails):
                result, _ = self.render_kubernetes_job(
                    cluster_ip=cluster_ip, lookup_fails=lookup_fails,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("ClusterIP", result.stderr)
                self.assertEqual(result.stdout, "")

    def test_explicit_local_ip_and_custom_service_dns_remain_supported(self) -> None:
        for variables, aliases in (
            (("AIPERF_CONNECT_IP=192.168.1.235",),
             [{"ip": "192.168.1.235", "hostnames": ["daedalus.ddns.me"]}]),
            (("AIPERF_CONNECT_IP=",), []),
            (("AIPERF_ENDPOINT_URL=http://custom-gateway.test.svc.cluster.local:8000",), []),
        ):
            with self.subTest(variables=variables):
                result, calls = self.render_kubernetes_job(*variables)
                self.assertEqual(result.returncode, 0, result.stderr)
                pod = json.loads(result.stdout)["spec"]["template"]["spec"]
                self.assertEqual(pod.get("hostAliases", []), aliases)
                self.assertEqual(calls, "")

    def test_kubernetes_runner_copies_results_before_deleting_job(self) -> None:
        self.check_kubernetes_runner()

    def test_failed_benchmark_still_copies_results(self) -> None:
        self.check_kubernetes_runner(benchmark_exit_code=7)

    def test_failed_copy_keeps_container_and_job_for_recovery(self) -> None:
        self.check_kubernetes_runner(copy_fails=True)

    def check_kubernetes_runner(
        self, benchmark_exit_code: int = 0, copy_fails: bool = False
    ) -> None:
        temp_root = ROOT / "tmp"
        temp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_root) as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            fake_kubectl = temp_dir / "kubectl"
            call_log = temp_dir / "kubectl-calls.log"
            fake_kubectl.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    set -euo pipefail
                    printf '%s\\n' "$*" >>"${FAKE_KUBECTL_LOG}"
                    if [[ "$1" == "config" && "$2" == "current-context" ]]; then
                      printf '%s\\n' 'test-context'
                    elif [[ "$1" == "get" && "$2" == "namespace" ]]; then
                      exit 0
                    elif [[ "$1" == "get" && "$2" == "service" ]]; then
                      if [[ "$*" == *jsonpath* ]]; then printf '%s' '32359'; fi
                    elif [[ "$1" == "create" ]]; then
                      printf '%s\\n' 'job.batch/aiperf-agentic-unit-k8s-run created'
                    elif [[ "$1" == "get" && "$2" == "job" ]]; then
                      if [[ "$*" == *Complete* ]]; then
                        [[ -f "${FAKE_KUBECTL_LOG}.released" ]] && printf '%s' 'True'
                      elif [[ "$*" == *Failed* ]]; then
                        printf '%s' ''
                      else
                        printf '%s\\n' '{"status":{"succeeded":1}}'
                      fi
                    elif [[ "$1" == "get" && "$2" == "pods" ]]; then
                      printf '%s' 'aiperf-agentic-unit-k8s-run-pod'
                    elif [[ "$1" == "get" && "$2" == "pod" ]]; then
                      printf '%s\\n' '{"status":{"phase":"Running"}}'
                    elif [[ "$1" == "exec" ]]; then
                      if [[ "$*" == *benchmark-exit-code ]]; then
                        printf '%s' "${FAKE_BENCHMARK_EXIT_CODE}"
                      elif [[ "$*" == *artifacts-collected ]]; then
                        touch "${FAKE_KUBECTL_LOG}.released"
                      else
                        exit 2
                      fi
                    elif [[ "$1" == "logs" ]]; then
                      printf '%s\\n' 'benchmark complete'
                    elif [[ "$1" == "cp" ]]; then
                      # Model kubectl cp: it cannot exec tar after container exit.
                      [[ ! -f "${FAKE_KUBECTL_LOG}.released" ]] || exit 2
                      [[ "${FAKE_COPY_FAILS}" == "0" ]] || exit 1
                      destination="${@: -1}"
                      mkdir -p "${destination}"
                      printf '%s\\n' '{"run_id":"unit-k8s-run"}' >"${destination}/run-manifest.json"
                      printf '%s\\n' \
                        '{"completed_models":2,"failed_models":0}' \
                        >"${destination}/run-summary.json"
                    elif [[ "$1" == "delete" && "$2" == "job" ]]; then
                      printf '%s\\n' 'job.batch deleted'
                    else
                      printf 'unexpected fake kubectl call: %s\\n' "$*" >&2
                      exit 2
                    fi
                    """
                ),
                encoding="utf-8",
            )
            fake_kubectl.chmod(0o755)

            relative_results = temp_dir.relative_to(ROOT) / "results"
            env = os.environ.copy()
            env.update(
                {
                    "KUBECTL": str(fake_kubectl),
                    "PYTHON": sys.executable,
                    "FAKE_KUBECTL_LOG": str(call_log),
                    "FAKE_BENCHMARK_EXIT_CODE": str(benchmark_exit_code),
                    "FAKE_COPY_FAILS": "1" if copy_fails else "0",
                    "EXPECTED_CONTEXT": "test-context",
                    "NAMESPACE": "daedalus-dynamo",
                    "GATEWAY_NAME": "inference-gateway",
                    "GATEWAY_PORT": "32359",
                    "AIPERF_IMAGE": "registry.example.com/aiperf:0.13.0",
                    "AIPERF_ENDPOINT_URL": (
                        "http://inference-gateway.daedalus-dynamo."
                        "svc.cluster.local:32359"
                    ),
                    "AIPERF_RUN_ID": "unit-k8s-run",
                    "AIPERF_RESULTS_DIR": str(relative_results),
                    "AIPERF_ACTIVE_DEADLINE_SECONDS": "10",
                    "AIPERF_BENCHMARK_DURATION_SECONDS": "0",
                    "AIPERF_POLL_SECONDS": "1",
                }
            )
            result = subprocess.run(
                [str(KUBERNETES_RUNNER)],
                cwd=ROOT,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )

            run_dir = ROOT / relative_results / "unit-k8s-run"
            summary = json.loads(
                (run_dir / "kubernetes" / "orchestration-summary.json").read_text()
            )
            calls = call_log.read_text().splitlines()
            if copy_fails:
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(summary["artifacts"]["copied"])
                self.assertFalse(summary["kubernetes"]["job_deleted"])
                self.assertFalse(any("artifacts-collected" in call for call in calls))
                self.assertFalse(any(call.startswith("delete job ") for call in calls))
                return

            self.assertEqual(result.returncode, 0 if benchmark_exit_code == 0 else 1)
            self.assertEqual(
                summary["kubernetes"]["result"],
                "completed" if benchmark_exit_code == 0 else "failed",
            )
            self.assertTrue((run_dir / "workload" / "run-manifest.json").is_file())
            self.assertTrue((run_dir / "kubernetes" / "job.log").is_file())
            self.assertTrue(
                (run_dir / "kubernetes" / "workload-sha256.txt").is_file()
            )
            self.assertTrue(summary["artifacts"]["copied"])
            self.assertTrue(summary["kubernetes"]["job_deleted"])
            self.assertIn("Local benchmark evidence", result.stdout)

            copy_index = next(
                index for index, call in enumerate(calls) if call.startswith("cp ")
            )
            delete_index = next(
                index for index, call in enumerate(calls) if call.startswith("delete job ")
            )
            self.assertLess(copy_index, delete_index)
            release_index = next(
                index for index, call in enumerate(calls)
                if "artifacts-collected" in call
            )
            self.assertLess(copy_index, release_index)
            self.assertLess(release_index, delete_index)

    def test_container_waits_for_copy_and_preserves_benchmark_exit_code(self) -> None:
        for exit_code in (0, 7):
            with self.subTest(exit_code=exit_code), tempfile.TemporaryDirectory() as temp:
                temp_dir = Path(temp)
                benchmark = temp_dir / "benchmark.sh"
                benchmark.write_text(
                    '#!/usr/bin/env bash\n'
                    '[[ ! -e "${ARTIFACTS_DIR}/${RUN_ID}" ]] || exit 99\n'
                    'mkdir -p "${ARTIFACTS_DIR}/${RUN_ID}"\n'
                    'printf "{}\\n" >"${ARTIFACTS_DIR}/${RUN_ID}/run-manifest.json"\n'
                    f'exit {exit_code}\n'
                )
                benchmark.chmod(0o755)
                env = os.environ.copy()
                env.update({
                    "ARTIFACTS_DIR": temp,
                    "RUN_ID": "container-test",
                    "AIPERF_BENCHMARK_SCRIPT": str(benchmark),
                })
                process = subprocess.Popen(
                    ["bash", str(BENCHMARK_DIR / "run-kubernetes-container.sh")],
                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                try:
                    run_dir = temp_dir / "container-test"
                    marker = run_dir / "benchmark-exit-code"
                    deadline = time.monotonic() + 5
                    while not marker.exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertEqual(marker.read_text().strip(), str(exit_code))
                    self.assertIsNone(process.poll())
                    self.assertTrue((run_dir / "run-manifest.json").exists())
                    (run_dir / "artifacts-collected").touch()
                    process.communicate(timeout=5)
                    self.assertEqual(process.returncode, exit_code)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
