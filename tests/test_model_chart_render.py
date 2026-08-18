from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "charts" / "dynamo-model"
MODEL_VALUES = sorted((ROOT / "values").glob("*.yaml"))
SHARED_RUNTIME_IMAGE = (
    "daedalus.ddns.me:5050/btuttle/daedalus-dynamo-vllm-runtime:"
    "20260717-5930e58"
)


def render(
    values_file: Path,
    deployment_mode: str | None = None,
    set_strings: dict[str, str] | None = None,
) -> list[dict]:
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
    for key, value in (set_strings or {}).items():
        command.extend(["--set-string", f"{key}={value}"])

    completed = subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [document for document in yaml.safe_load_all(completed.stdout) if document]


class ModelChartRenderTests(unittest.TestCase):
    def test_all_vllm_models_share_the_source_built_runtime(self) -> None:
        for values_file in MODEL_VALUES:
            with self.subTest(model=values_file.stem):
                values = yaml.safe_load(values_file.read_text())
                if values["model"].get("backendFramework", "vllm") != "vllm":
                    continue
                self.assertEqual(values["model"]["image"], SHARED_RUNTIME_IMAGE)

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

    def test_worker_gpu_memory_selector_supports_gt_and_lt(self) -> None:
        cases = {
            "nemotronnano.yaml": ("Gt", "40959"),
            "glm47flash.yaml": ("Gt", "40959"),
        }

        for values_name, expected in cases.items():
            with self.subTest(values=values_name):
                documents = render(ROOT / "values" / values_name)
                deployment = next(
                    document
                    for document in documents
                    if document["kind"] == "DynamoGraphDeployment"
                )
                worker = deployment["spec"]["services"]["VllmWorker"]
                expressions = worker["extraPodSpec"]["affinity"]["nodeAffinity"][
                    "requiredDuringSchedulingIgnoredDuringExecution"
                ]["nodeSelectorTerms"][0]["matchExpressions"]
                gpu_memory = next(
                    expression
                    for expression in expressions
                    if expression["key"] == "nvidia.com/gpu.memory"
                )

                self.assertEqual(gpu_memory["operator"], expected[0])
                self.assertEqual(gpu_memory["values"], [expected[1]])

        documents = render(
            ROOT / "values" / "qwen27b.yaml",
            set_strings={"worker.nodeGpuMemoryLt": "40959"},
        )
        deployment = next(
            document
            for document in documents
            if document["kind"] == "DynamoGraphDeployment"
        )
        worker = deployment["spec"]["services"]["VllmWorker"]
        expressions = worker["extraPodSpec"]["affinity"]["nodeAffinity"][
            "requiredDuringSchedulingIgnoredDuringExecution"
        ]["nodeSelectorTerms"][0]["matchExpressions"]
        gpu_memory = next(
            expression
            for expression in expressions
            if expression["key"] == "nvidia.com/gpu.memory"
        )

        self.assertEqual(gpu_memory["operator"], "Lt")
        self.assertEqual(gpu_memory["values"], ["40959"])

    def test_frontend_receives_configured_cli_flags(self) -> None:
        documents = render(ROOT / "values" / "gemma431b.yaml")
        deployment = next(
            document
            for document in documents
            if document["kind"] == "DynamoGraphDeployment"
        )
        frontend = deployment["spec"]["services"]["Frontend"]
        script = frontend["extraPodSpec"]["mainContainer"]["args"][0]

        self.assertIn("exec python3 -m dynamo.frontend \\\n", script)
        self.assertIn("--router-mode kv \\\n", script)
        self.assertIn("--router-min-initial-workers 1 \\\n", script)

    def test_modelexpress_resets_only_registry_metadata_before_download(self) -> None:
        documents = render(ROOT / "values" / "gemma431b.yaml")
        deployment = next(
            document
            for document in documents
            if document["kind"] == "DynamoGraphDeployment"
        )
        modelexpress = deployment["spec"]["services"]["ModelExpress"]
        script = modelexpress["extraPodSpec"]["mainContainer"]["args"][0]

        self.assertIn(
            'MODEL_REGISTRY_KEY="mx:model:HuggingFace:${MODEL_NAME}"', script
        )
        self.assertIn("DEL\\r\\n", script)
        self.assertIn('unlink "$MODEL_READY_LINK"', script)
        self.assertLess(
            script.index('unlink "$MODEL_READY_LINK"'),
            script.index("Starting Model Express Server"),
        )
        self.assertIn('model download "$MODEL_NAME" --strategy server-only', script)

        worker = deployment["spec"]["services"]["VllmWorker"]
        worker_script = worker["extraPodSpec"]["mainContainer"]["args"][0]
        self.assertIn(
            'MODEL_READY_LINK="$MODEL_SNAPSHOTS_PARENT/latest"', worker_script
        )
        self.assertIn(
            'while [ ! -L "$MODEL_READY_LINK" ] || [ ! -d "$MODEL_READY_LINK" ]',
            worker_script,
        )

    def test_qwen35_uses_native_modelopt_support_in_vllm_025(self) -> None:
        documents = render(ROOT / "values" / "qwen35b.yaml")
        deployment = next(
            document
            for document in documents
            if document["kind"] == "DynamoGraphDeployment"
        )
        worker = deployment["spec"]["services"]["VllmWorker"]
        worker_script = worker["extraPodSpec"]["mainContainer"]["args"][0]

        self.assertNotIn("--load-format mx", worker_script)
        self.assertIn("--quantization modelopt", worker_script)
        self.assertNotIn('expected_version = "0.22.0"', worker_script)
        self.assertNotIn("Applying vLLM 0.22.0", worker_script)

    def test_nemotron35lightning_gb10_uses_trtllm_and_modelexpress(self) -> None:
        documents = render(
            ROOT / "values" / "nemotron35lightning-gb10.yaml"
        )
        deployment = next(
            document
            for document in documents
            if document["kind"] == "DynamoGraphDeployment"
        )
        services = deployment["spec"]["services"]
        worker = services["TrtllmWorker"]
        main_container = worker["extraPodSpec"]["mainContainer"]
        worker_script = main_container["args"][0]

        self.assertEqual(deployment["spec"]["backendFramework"], "trtllm")
        self.assertEqual(worker["replicas"], 1)
        self.assertFalse(worker["scalingAdapter"]["enabled"])
        self.assertEqual(
            worker["extraPodSpec"]["nodeSelector"],
            {"nvidia.com/gpu.product": "NVIDIA-GB10"},
        )
        self.assertNotIn("affinity", worker["extraPodSpec"])
        self.assertEqual(worker["resources"]["requests"]["memory"], "96Gi")
        self.assertEqual(worker["sharedMemory"]["size"], "16Gi")
        self.assertEqual(main_container["startupProbe"]["httpGet"]["path"], "/live")
        self.assertIn("exec python3 -m dynamo.trtllm", worker_script)
        self.assertNotIn("python3 -m dynamo.vllm", worker_script)
        self.assertIn("max_batch_size: 8", worker_script)
        self.assertIn("max_num_tokens: 32768", worker_script)
        self.assertIn('"8"', worker_script)
        self.assertIn('"32768"', worker_script)
        self.assertNotIn("Planner", services)

        modelexpress = services["ModelExpress"]
        modelexpress_script = modelexpress["extraPodSpec"]["mainContainer"][
            "args"
        ][0]
        self.assertIn(
            'model download "$MODEL_NAME" --strategy server-only',
            modelexpress_script,
        )
        self.assertIn("Waiting for ModelExpress", worker_script)

    def test_glm47flash_uses_vllm_flags_and_l40s_safe_baseline(self) -> None:
        documents = render(ROOT / "values" / "glm47flash.yaml")
        deployment = next(
            document
            for document in documents
            if document["kind"] == "DynamoGraphDeployment"
        )
        worker = deployment["spec"]["services"]["VllmWorker"]
        frontend = deployment["spec"]["services"]["Frontend"]
        main_container = worker["extraPodSpec"]["mainContainer"]
        worker_script = main_container["args"][0]
        worker_env = {
            item["name"]: item["value"] for item in main_container["env"]
        }

        self.assertEqual(worker_env["MODEL_NAME"], "unsloth/GLM-4.7-Flash-NVFP4")
        self.assertEqual(worker_env["VLLM_ATTENTION_BACKEND"], "TRITON_MLA")
        self.assertEqual(
            frontend["extraPodSpec"]["nodeSelector"]["nvidia.com/gpu.product"],
            "NVIDIA-L40S",
        )
        self.assertIn("--dyn-tool-call-parser glm47", worker_script)
        self.assertIn("--dyn-reasoning-parser glm45", worker_script)
        self.assertIn("--max-model-len 32768", worker_script)
        self.assertNotIn("--frontend-decoding", worker_script)
        self.assertNotIn("--trust-remote-code", worker_script)
        self.assertNotIn("--speculative-config", worker_script)
        self.assertNotIn("--speculative-eagle-topk", worker_script)
        self.assertNotIn("--speculative-num-draft-tokens", worker_script)
        self.assertNotIn("--speculative-algorithm", worker_script)
        self.assertNotIn("--speculative-num-steps", worker_script)
        self.assertNotIn("--moe-backend marlin", worker_script)
        self.assertNotIn("--quantization modelopt", worker_script)
        self.assertNotIn("--kv-cache-dtype fp8", worker_script)


if __name__ == "__main__":
    unittest.main()
