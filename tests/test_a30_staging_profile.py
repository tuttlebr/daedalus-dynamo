from __future__ import annotations

import shlex
import subprocess
import unittest

from test_model_chart_render import QWEN38_L40S_RUNTIME_IMAGE, ROOT, render


class A30StagingProfileTests(unittest.TestCase):
    def test_isolated_int4_text_worker_and_retained_storage(self) -> None:
        documents = render(ROOT / "tests/fixtures/retired-values" / "staging" / "qwen36-35b-int4-a30.yaml")
        deployment = next(d for d in documents if d["kind"] == "DynamoGraphDeployment")
        self.assertEqual(deployment["metadata"]["name"], "qwen36-35b-int4-a30")
        self.assertEqual(
            deployment["metadata"]["annotations"],
            {"nvidia.com/enable-grove": "false"},
        )
        services = deployment["spec"]["services"]
        self.assertEqual(set(services), {"Frontend", "ModelExpress", "VllmWorker"})
        for service in services.values():
            self.assertEqual(
                service["extraPodSpec"]["nodeSelector"],
                {"kubernetes.io/hostname": "daedalus-04"},
            )
            self.assertIn(
                {"name": "qwen36-35b-int4-a30-nfs-model-cache", "mountPoint": "/model"},
                service["volumeMounts"],
            )
        worker = services["VllmWorker"]
        self.assertEqual(worker["replicas"], 1)
        self.assertFalse(worker["scalingAdapter"]["enabled"])
        self.assertEqual(worker["resources"]["requests"]["gpu"], "1")
        self.assertEqual(worker["resources"]["limits"]["gpu"], "1")
        self.assertEqual(worker["resources"]["requests"]["memory"], "64Gi")
        self.assertEqual(worker["resources"]["limits"]["memory"], "80Gi")
        container = worker["extraPodSpec"]["mainContainer"]
        self.assertEqual(container["image"], QWEN38_L40S_RUNTIME_IMAGE)
        environment = {e["name"]: e["value"] for e in container["env"]}
        self.assertEqual(
            environment["MODEL_NAME"], "Intel/Qwen3.6-35B-A3B-int4-mixed-AutoRound"
        )
        script = container["args"][0]
        for flag, value in (
            ("--revision", "65f69c73f17488236c85c85211f6ba28d7106157"),
            ("--quantization", "inc"),
            ("--moe-backend", "marlin"),
            ("--tensor-parallel-size", "1"),
            ("--pipeline-parallel-size", "1"),
            ("--data-parallel-size", "1"),
            ("--dtype", "bfloat16"),
            ("--max-model-len", "131072"),
            ("--max-num-seqs", "1"),
            ("--max-num-batched-tokens", "512"),
            ("--kv-cache-memory-bytes", "3221225472"),
            ("--kv-cache-dtype", "auto"),
        ):
            self.assertIn(f"{flag} {value}", script)
        for flag in ("--enforce-eager", "--language-model-only", "--no-enable-prefix-caching"):
            self.assertIn(flag, script)
        for flag in ("--speculative", "--trust-remote-code", "--enable-multimodal"):
            self.assertNotIn(flag, script)
        subprocess.run(["bash", "-n"], input=script, text=True, check=True)
        frontend = services["Frontend"]["extraPodSpec"]["mainContainer"]
        self.assertEqual(frontend["image"], QWEN38_L40S_RUNTIME_IMAGE)
        self.assertEqual(frontend["resources"]["requests"]["memory"], "4Gi")
        self.assertEqual(frontend["resources"]["limits"]["memory"], "8Gi")
        command = frontend["args"][0].split("exec python3 -m dynamo.frontend", 1)[1]
        argv = shlex.split(command.replace("\\\n", ""))
        self.assertEqual(argv[argv.index("--tool-call-parser") + 1], "qwen3_coder")
        self.assertEqual(argv[argv.index("--reasoning-parser") + 1], "qwen3")
        for component in services.values():
            self.assertIn("@sha256:", component["extraPodSpec"]["mainContainer"]["image"])
        claims = [d for d in documents if d["kind"] == "PersistentVolumeClaim"]
        self.assertEqual(len(claims), 2)
        for claim in claims:
            self.assertTrue(claim["metadata"]["name"].startswith("qwen36-35b-int4-a30-"))
            self.assertEqual(claim["metadata"]["annotations"]["helm.sh/resource-policy"], "keep")
        claim_specs = {c["metadata"]["name"]: c["spec"] for c in claims}
        cache = claim_specs["qwen36-35b-int4-a30-nfs-model-cache"]
        self.assertEqual(cache["storageClassName"], "nfs-client")
        self.assertEqual(cache["accessModes"], ["ReadWriteMany"])
        self.assertEqual(cache["resources"]["requests"]["storage"], "64Gi")
        redis = claim_specs["qwen36-35b-int4-a30-redis-data"]
        self.assertEqual(redis["storageClassName"], "local-fast")
        self.assertEqual(redis["accessModes"], ["ReadWriteOnce"])
        self.assertEqual(redis["resources"]["requests"]["storage"], "1Gi")
        for document in documents:
            self.assertNotIn(document["kind"], {"Ingress", "HTTPRoute", "Gateway"})
            if document["kind"] == "Service":
                self.assertEqual(document["spec"].get("type", "ClusterIP"), "ClusterIP")
                self.assertFalse(any("nodePort" in p for p in document["spec"]["ports"]))


if __name__ == "__main__":
    unittest.main()
