from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "charts" / "dynamo-model"
MODEL_VALUES = sorted((ROOT / "values").glob("*.yaml"))


def render(values_file: Path, deployment_mode: str | None = None) -> list[dict]:
    command = [
        "helm",
        "template",
        values_file.stem,
        str(CHART),
        "--namespace",
        "daedalus-dynamo",
        "--values",
        str(values_file),
    ]
    if deployment_mode:
        command.extend(["--set-string", f"model.deploymentMode={deployment_mode}"])

    completed = subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [document for document in yaml.safe_load_all(completed.stdout) if document]


class ModelChartRenderTests(unittest.TestCase):
    def test_direct_dgd_uses_alpha_services_schema(self) -> None:
        for values_file in MODEL_VALUES:
            with self.subTest(model=values_file.stem):
                documents = render(values_file)
                deployments = [
                    document
                    for document in documents
                    if document["kind"] == "DynamoGraphDeployment"
                ]
                self.assertEqual(len(deployments), 1)

                deployment = deployments[0]
                spec = deployment["spec"]
                self.assertEqual(deployment["apiVersion"], "nvidia.com/v1alpha1")
                self.assertIsInstance(spec.get("services"), dict)
                self.assertTrue(spec["services"])
                self.assertIsInstance(spec.get("pvcs"), list)
                self.assertNotIn("components", spec)

    def test_dgdr_embeds_alpha_dgd_override(self) -> None:
        for values_file in MODEL_VALUES:
            with self.subTest(model=values_file.stem):
                documents = render(values_file, deployment_mode="dgdr")
                requests = [
                    document
                    for document in documents
                    if document["kind"] == "DynamoGraphDeploymentRequest"
                ]
                self.assertEqual(len(requests), 1)

                request = requests[0]
                override = request["spec"]["overrides"]["dgd"]
                self.assertEqual(request["apiVersion"], "nvidia.com/v1beta1")
                self.assertEqual(override["apiVersion"], "nvidia.com/v1alpha1")
                self.assertIsInstance(override["spec"].get("services"), dict)
                self.assertTrue(override["spec"]["services"])

    def test_nemotronnano_uses_a_queue_first_latency_guardrail(self) -> None:
        documents = render(ROOT / "values" / "nemotronnano.yaml")
        deployment = next(
            document
            for document in documents
            if document["kind"] == "DynamoGraphDeployment"
        )
        services = deployment["spec"]["services"]
        planner_args = services["Planner"]["extraPodSpec"]["mainContainer"]["args"]
        planner_config = json.loads(planner_args[planner_args.index("--config") + 1])

        self.assertEqual(planner_config["optimization_target"], "load")
        self.assertTrue(planner_config["enable_load_scaling"])
        self.assertFalse(planner_config["enable_throughput_scaling"])
        self.assertEqual(planner_config["decode_scale_up_kv_rate"], 0.25)
        self.assertEqual(planner_config["decode_scale_down_kv_rate"], 0.05)
        self.assertEqual(planner_config["min_endpoint"], 2)
        self.assertGreaterEqual(
            planner_config["max_gpu_budget"], planner_config["min_endpoint"]
        )

        worker = services["VllmWorker"]
        self.assertTrue(worker["scalingAdapter"]["enabled"])


if __name__ == "__main__":
    unittest.main()
