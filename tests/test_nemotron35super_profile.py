"""Local render/generator checks only; no model, network or Kubernetes calls."""

import ast
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
VALUES = ROOT / "values/staging/nemotron35super-gb10.yaml"
NAME = "nemotron35super-gb10-stage"


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


POST = load("super_postrender", "nemotron35super_gb10_postrender.py")
PREPARE = load("super_prepare", "prepare_nemotron35super_cache.py")


class SuperProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raw = subprocess.check_output(
            [
                "helm",
                "template",
                NAME,
                str(ROOT / "charts/dynamo-model"),
                "--namespace",
                "daedalus-dynamo",
                "--values",
                str(VALUES),
            ],
            text=True,
            timeout=30,
        )
        cls.original = [item for item in yaml.safe_load_all(raw) if item]
        cls.rendered = POST.adapt(cls.original)

    def graph(self):
        return next(
            item for item in self.rendered if item["kind"] == "DynamoGraphDeployment"
        )

    def test_only_new_private_service_and_target_graph_are_adapted(self):
        original = copy.deepcopy(self.original)
        rendered = POST.adapt(self.original)
        self.assertEqual(self.original, original)
        before = [item for item in original if item["kind"] != "DynamoGraphDeployment"]
        after = [
            item
            for item in rendered
            if item["kind"] not in {"DynamoGraphDeployment", "Service"}
        ]
        self.assertEqual(before, after)
        services = [item for item in rendered if item["kind"] == "Service"]
        self.assertEqual(len(services), 1)
        service = services[0]
        self.assertEqual(service["metadata"]["name"], NAME + "-private")
        self.assertEqual(service["metadata"]["namespace"], "daedalus-dynamo")
        self.assertEqual(service["spec"]["type"], "ClusterIP")
        self.assertEqual(
            service["spec"]["selector"]["nvidia.com/dynamo-namespace"],
            "daedalus-dynamo-" + NAME,
        )
        self.assertNotIn("nodePort", json.dumps(service))
        self.assertFalse(
            any(item["kind"] in {"HTTPRoute", "Ingress", "Secret"} for item in rendered)
        )

    def test_exact_offline_text_only_worker_and_fail_closed_startup(self):
        graph = self.graph()
        self.assertEqual(set(graph["spec"]["services"]), {"Frontend", "VllmWorker"})
        for name, service in graph["spec"]["services"].items():
            self.assertNotIn("envFromSecret", service)
            container = service["extraPodSpec"]["mainContainer"]
            self.assertNotIn("envFrom", container)
            self.assertNotIn("secretKeyRef", json.dumps(container))
            env = {entry["name"]: entry.get("value") for entry in container["env"]}
            for flag in (
                "HF_HUB_OFFLINE",
                "TRANSFORMERS_OFFLINE",
                "HF_HUB_DISABLE_IMPLICIT_TOKEN",
            ):
                self.assertEqual(env[flag], "1")
            script = container["args"][0]
            subprocess.run(
                ["bash", "-n"], input=script, text=True, check=True, timeout=5
            )
            embedded = script.split("python3 -I - <<'CHECK'\n", 1)[1].split(
                "\nCHECK\n", 1
            )[0]
            ast.parse(embedded)
            shared = POST.INTEGRITY_SOURCE.read_text().split(
                "# DOWNLOAD_ENTRYPOINT", 1
            )[0]
            self.assertTrue(embedded.startswith(shared))
            self.assertLess(
                script.index("validate_offline(Path("),
                script.index("exec python3 -m dynamo."),
            )
            self.assertIn(POST.SNAPSHOT, embedded)
            if name == "VllmWorker":
                self.assertNotIn("Waiting for ModelExpress", script)
                for option in (
                    "--model " + json.dumps(POST.SNAPSHOT),
                    "--tokenizer " + json.dumps(POST.SNAPSHOT),
                    "--revision " + POST.REVISION,
                    "--max-model-len 131072",
                    "--max-num-seqs 1",
                    "--max-num-batched-tokens 2048",
                    "--kv-cache-memory-bytes 8589934592",
                    "--language-model-only",
                    "--enforce-eager",
                    "--moe-backend marlin",
                    "--no-enable-prefix-caching",
                    "--mamba-backend flashinfer",
                ):
                    self.assertIn(option, script)
                self.assertIn("--limit-mm-per-prompt", script)
                self.assertNotIn("--speculative-config", script)
                self.assertNotIn(
                    '--served-model-name "nvidia/NVIDIA-Nemotron-3.5-Bolt', script
                )

    def test_cache_job_uses_exact_shared_producer_and_bounded_cpu_resources(self):
        pvc, job = PREPARE.render()
        spec = job["spec"]["template"]["spec"]
        container = spec["containers"][0]
        self.assertEqual(
            container["command"],
            ["python3", "-I", "-c", POST.INTEGRITY_SOURCE.read_text()],
        )
        self.assertEqual(job["spec"]["backoffLimit"], 0)
        self.assertEqual(job["spec"]["activeDeadlineSeconds"], 14400)
        self.assertFalse(spec["automountServiceAccountToken"])
        self.assertNotIn("nvidia.com/gpu", container["resources"]["limits"])
        self.assertEqual(container["resources"]["limits"]["memory"], "6Gi")
        self.assertEqual(spec["securityContext"]["runAsUser"], 1000)
        self.assertNotIn("secretKeyRef", json.dumps(job))
        self.assertNotIn("envFrom", container)
        self.assertEqual(
            spec["volumes"][0]["persistentVolumeClaim"]["claimName"],
            pvc["metadata"]["name"],
        )
        self.assertEqual(pvc["spec"]["resources"]["requests"]["storage"], "128Gi")
        self.assertEqual(
            pvc["metadata"]["annotations"]["meta.helm.sh/release-name"], NAME
        )
        with self.assertRaises(ValueError):
            PREPARE.render("another-namespace")

    def test_worker_health_check_allows_long_prefill_without_masking_probes(self):
        services = self.graph()["spec"]["services"]
        worker_service = services["VllmWorker"]
        worker = worker_service["extraPodSpec"]["mainContainer"]
        frontend = services["Frontend"]["extraPodSpec"]["mainContainer"]
        key = "DYN_HEALTH_CHECK_REQUEST_TIMEOUT"
        env = {item["name"]: item.get("value") for item in worker["env"]}
        self.assertEqual(env.get(key), "120")
        self.assertNotIn(key, {item["name"] for item in frontend["env"]})
        self.assertEqual(worker_service["readinessProbe"]["httpGet"]["path"], "/health")
        self.assertEqual(worker_service["livenessProbe"]["httpGet"]["path"], "/live")
        self.assertEqual(worker_service["livenessProbe"]["failureThreshold"], 30)

    def test_wrong_graph_or_extra_component_fails_closed(self):
        for mutate in (
            lambda graph: graph["metadata"].update(name="unrelated-live-model"),
            lambda graph: graph["spec"]["services"].update(ModelExpress={}),
        ):
            objects = copy.deepcopy(self.original)
            graph = next(
                item for item in objects if item["kind"] == "DynamoGraphDeployment"
            )
            mutate(graph)
            with self.assertRaises(AssertionError):
                POST.adapt(objects)


if __name__ == "__main__":
    unittest.main()
