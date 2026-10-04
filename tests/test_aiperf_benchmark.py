from __future__ import annotations

import json
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


class AIPerfBenchmarkTests(unittest.TestCase):
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

        self.assertIn('"aiperf==0.11.0"', pyproject)
        self.assertIn('org.opencontainers.image.version="0.11.0"', dockerfile)
        self.assertIn('EXPECTED_AIPERF_VERSION="0.11.0"', script)
        self.assertIn('MOONCAKE_BLOCK_SIZE="512"', script)
        self.assertIn(
            'AIPERF_SOURCE_COMMIT="38687855e98044fcf12ee48c6794128f10b6780b"',
            script,
        )
        self.assertIn("output_length_evidence", script)

    def test_default_concurrency_matches_three_actor_sla(self) -> None:
        script = SCRIPT.read_text()
        compose = (BENCHMARK_DIR / "docker-compose.yaml").read_text()

        self.assertIn('CONCURRENCY="${CONCURRENCY:-3}"', script)
        self.assertIn('CONCURRENCY: "${CONCURRENCY:-3}"', compose)

    def test_agentic_dataset_has_six_growing_turns(self) -> None:
        config = json.loads((BENCHMARK_DIR / "agentic-code-config.json").read_text())

        self.assertEqual(config["turns"]["mean"], 6)
        self.assertEqual(config["turns"]["min"], 6)
        self.assertEqual(config["turns"]["max"], 6)
        self.assertEqual(config["block_size"], 512)
        self.assertEqual(config["cache"]["layer1_tokens"], 1024)
        self.assertEqual(config["cache"]["layer1_5_tokens"], 512)

    def test_make_renders_bounded_all_model_kubernetes_job(self) -> None:
        result = subprocess.run(
            [
                "make",
                "--no-print-directory",
                "benchmark-agentic-render",
                "AIPERF_RUN_ID=unit-k8s-render",
                "AIPERF_IMAGE=registry.example.com/aiperf:0.11.0",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
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
            "http://inference-gateway.daedalus-dynamo.svc.cluster.local:32359",
        )
        self.assertEqual(environment["CONCURRENCY"]["value"], "3")
        self.assertEqual(environment["RUN_ID"]["value"], "unit-k8s-render")
        self.assertNotIn("MODELS", environment)
        self.assertNotIn("OPENAI_API_KEY", environment)

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
                    "AIPERF_IMAGE": "registry.example.com/aiperf:0.11.0",
                    "AIPERF_ENDPOINT_URL": (
                        "http://inference-gateway.daedalus-dynamo."
                        "svc.cluster.local:32359"
                    ),
                    "AIPERF_RUN_ID": "unit-k8s-run",
                    "AIPERF_RESULTS_DIR": str(relative_results),
                    "AIPERF_ACTIVE_DEADLINE_SECONDS": "10",
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
