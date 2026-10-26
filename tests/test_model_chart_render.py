from __future__ import annotations

import json
import re
import shlex
import subprocess
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "charts" / "dynamo-model"
MODEL_VALUES = sorted((ROOT / "values").glob("*.yaml"))
SHARED_RUNTIME_IMAGE = (
    "nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.4.1"
)
GB10_RUNTIME_IMAGE = "nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.4.1"
NEMOTRON35_BOLT_RUNTIME_IMAGE = (
    "daedalus.ddns.me:5050/btuttle/daedalus-dynamo-nemotron35bolt-gb10:"
    "1.6.0dev20261019-vllm77154ca-mamba-prefill-20261026-2"
)
NEMOTRON35_LIGHTNING_RUNTIME_IMAGE = (
    "nvcr.io/nvidia/ai-dynamo/vllm-runtime:"
    "1.5.0-nemotron-3.5-lightning-dev.1"
)
MUSE_GLIMMER_RUNTIME_IMAGE = (
    "nvcr.io/nvidia/ai-dynamo/vllm-runtime:"
    "1.5.0-deepseek-v4-pro-0813-dev.1"
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
    def test_vllm_models_use_platform_compatible_runtime(self) -> None:
        for values_file in MODEL_VALUES:
            with self.subTest(model=values_file.stem):
                values = yaml.safe_load(values_file.read_text())
                if values["model"].get("backendFramework", "vllm") != "vllm":
                    continue
                if values_file.name == "qwen35-122b-gb10.yaml":
                    expected_image = GB10_RUNTIME_IMAGE
                elif values_file.name == "nemotron35bolt-gb10.yaml":
                    expected_image = NEMOTRON35_BOLT_RUNTIME_IMAGE
                elif values_file.name == "nemotron35lightning-l40s.yaml":
                    expected_image = NEMOTRON35_LIGHTNING_RUNTIME_IMAGE
                elif values_file.name == "muse-glimmer-l40s.yaml":
                    expected_image = MUSE_GLIMMER_RUNTIME_IMAGE
                else:
                    expected_image = SHARED_RUNTIME_IMAGE
                self.assertEqual(values["model"]["image"], expected_image)
                self.assertEqual(values["model"]["imagePullPolicy"], "IfNotPresent")

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
                self.assertEqual(request["spec"]["workload"]["concurrency"], 3)
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
        self.assertEqual(planner_config["min_endpoint"], 1)
        self.assertEqual(planner_config["max_gpu_budget"], 2)
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

    def test_frontend_extra_args_preserve_literal_argv_tokens(self) -> None:
        template_path = "/templates/a template's $(literal).jinja"
        documents = render(
            ROOT / "values" / "gemma431b.yaml",
            set_strings={
                "frontend.extraArgs[0]": "--chat-template",
                "frontend.extraArgs[1]": template_path,
            },
        )
        deployment = next(
            document
            for document in documents
            if document["kind"] == "DynamoGraphDeployment"
        )
        frontend = deployment["spec"]["services"]["Frontend"]
        script = frontend["extraPodSpec"]["mainContainer"]["args"][0]
        command = script.split("exec python3 -m dynamo.frontend", 1)[1]
        argv = shlex.split(command.replace("\\\n", ""))
        self.assertEqual(argv[argv.index("--chat-template") + 1], template_path)
        # All user-supplied text remains single-quoted, including substitution
        # syntax; embedded apostrophes temporarily use a double-quoted literal.
        self.assertIn("'\"'\"'", command)
        self.assertIn(" $(literal).jinja'", command)
        subprocess.run(["bash", "-n"], input=script, text=True, check=True)

    def test_muse_glimmer_starts_one_l40s_worker_with_frontend_parser_fallback(self) -> None:
        documents = render(ROOT / "values" / "muse-glimmer-l40s.yaml")
        deployment = next(
            document
            for document in documents
            if document["kind"] == "DynamoGraphDeployment"
        )
        services = deployment["spec"]["services"]
        self.assertNotIn("Planner", services)
        worker = services["VllmWorker"]
        self.assertEqual(worker["replicas"], 1)
        self.assertEqual(worker["resources"]["limits"]["gpu"], "1")
        self.assertFalse(worker["scalingAdapter"]["enabled"])
        self.assertEqual(
            worker["extraPodSpec"]["nodeSelector"]["nvidia.com/gpu.product"],
            "NVIDIA-L40S",
        )
        container = worker["extraPodSpec"]["mainContainer"]
        self.assertEqual(container["image"], MUSE_GLIMMER_RUNTIME_IMAGE)
        script = container["args"][0]
        self.assertIn("--tensor-parallel-size 1", script)
        self.assertIn("--max-model-len 131072", script)
        self.assertIn("--kv-cache-dtype auto", script)
        self.assertIn("--linear-backend auto", script)
        self.assertNotIn("--quantization modelopt_fp4", script)
        self.assertNotIn("--dyn-tool-call-parser", script)
        frontend_script = services["Frontend"]["extraPodSpec"]["mainContainer"][
            "args"
        ][0]
        command = frontend_script.split("exec python3 -m dynamo.frontend", 1)[1]
        argv = shlex.split(command.replace("\\\n", ""))
        for flag, value in (
            ("--router-min-initial-workers", "1"),
            ("--dyn-chat-processor", "vllm"),
            ("--tool-call-parser", "muse_glimmer"),
            ("--reasoning-parser", "muse_glimmer"),
        ):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertIn("--enable-auto-tool-choice", argv)

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

    def test_qwen35_uses_native_modelopt_support_in_vllm_023(self) -> None:
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

    def test_qwen35_122b_gb10_uses_arm64_profile_with_model_context(self) -> None:
        documents = render(ROOT / "values" / "qwen35-122b-gb10.yaml")
        deployment = next(
            document
            for document in documents
            if document["kind"] == "DynamoGraphDeployment"
        )
        services = deployment["spec"]["services"]
        worker = services["VllmWorker"]
        main_container = worker["extraPodSpec"]["mainContainer"]
        worker_script = main_container["args"][0]
        worker_env = {
            item["name"]: item["value"] for item in main_container["env"]
        }

        self.assertEqual(
            worker_env["MODEL_NAME"], "nvidia/Qwen3.5-122B-A10B-NVFP4"
        )
        self.assertEqual(main_container["image"], GB10_RUNTIME_IMAGE)
        self.assertEqual(worker["replicas"], 1)
        self.assertFalse(worker["scalingAdapter"]["enabled"])
        self.assertEqual(
            worker["extraPodSpec"]["nodeSelector"],
            {"nvidia.com/gpu.product": "NVIDIA-GB10"},
        )
        self.assertNotIn("affinity", worker["extraPodSpec"])
        self.assertEqual(worker["resources"]["requests"]["memory"], "96Gi")
        self.assertNotIn("Planner", services)

        self.assertEqual(worker_env["VLLM_NVFP4_GEMM_BACKEND"], "marlin")
        self.assertEqual(worker_env["VLLM_USE_FLASHINFER_MOE_FP4"], "0")
        self.assertEqual(worker_env["VLLM_SSM_CONV_STATE_LAYOUT"], "DS")
        self.assertIn("--quantization modelopt_fp4", worker_script)
        self.assertIn("--moe-backend marlin", worker_script)
        self.assertIn("--language-model-only", worker_script)
        self.assertNotIn("--max-model-len", worker_script)
        self.assertIn("--max-num-seqs 4", worker_script)
        self.assertIn("--max-num-batched-tokens 4096", worker_script)
        self.assertIn("--kv-cache-dtype fp8", worker_script)
        self.assertIn("--mamba-ssm-cache-dtype float16", worker_script)
        self.assertIn("--no-disable-hybrid-kv-cache-manager", worker_script)
        self.assertNotIn("--speculative-config", worker_script)

    def test_cluster_sized_shared_resources_and_planner(self) -> None:
        documents = render(ROOT / "values" / "gemma431b.yaml")
        deployment = next(
            document
            for document in documents
            if document["kind"] == "DynamoGraphDeployment"
        )
        services = deployment["spec"]["services"]

        self.assertEqual(services["Frontend"]["replicas"], 1)

        frontend_resources = services["Frontend"]["extraPodSpec"]["mainContainer"][
            "resources"
        ]
        self.assertEqual(
            frontend_resources["requests"]["memory"],
            "512Mi",
        )
        self.assertEqual(frontend_resources["requests"]["cpu"], "500m")
        modelexpress_resources = services["ModelExpress"]["extraPodSpec"][
            "mainContainer"
        ]["resources"]
        self.assertEqual(
            modelexpress_resources["requests"]["memory"],
            "1536Mi",
        )
        worker = services["VllmWorker"]
        self.assertEqual(worker["resources"]["requests"]["cpu"], "2")
        self.assertEqual(worker["resources"]["requests"]["memory"], "8Gi")
        self.assertEqual(worker["sharedMemory"]["size"], "16Gi")

        redis = next(
            document
            for document in documents
            if document["kind"] == "Deployment"
            and document["metadata"]["name"] == "gemma431b-redis"
        )
        redis_container = redis["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(redis_container["imagePullPolicy"], "IfNotPresent")
        self.assertEqual(
            redis_container["resources"]["requests"]["memory"], "64Mi"
        )

        planner_container = services["Planner"]["extraPodSpec"]["mainContainer"]
        self.assertEqual(
            planner_container["image"],
            "nvcr.io/nvidia/ai-dynamo/dynamo-planner:1.4.0",
        )
        self.assertEqual(
            planner_container["command"], ["python3", "-m", "dynamo.planner"]
        )
        self.assertEqual(
            planner_container["resources"]["requests"]["memory"], "384Mi"
        )

        disruption_budgets = {
            document["metadata"]["name"]: document
            for document in documents
            if document["kind"] == "PodDisruptionBudget"
        }
        self.assertEqual(
            set(disruption_budgets),
            {
                "gemma-4-31b-it-vllm-agg-frontend",
                "gemma-4-31b-it-vllm-agg-worker",
                "gemma431b-redis",
            },
        )
        for disruption_budget in disruption_budgets.values():
            self.assertEqual(disruption_budget["spec"]["minAvailable"], 1)

    def test_every_profile_admits_three_active_agent_turns(self) -> None:
        for values_file in MODEL_VALUES:
            with self.subTest(model=values_file.stem):
                documents = render(values_file)
                deployment = next(
                    document
                    for document in documents
                    if document["kind"] == "DynamoGraphDeployment"
                )
                services = deployment["spec"]["services"]
                self.assertEqual(services["Frontend"]["replicas"], 1)

                worker_name = next(
                    name for name in ("VllmWorker", "TrtllmWorker") if name in services
                )
                disruption_budget_selectors = [
                    document["spec"]["selector"]["matchLabels"]
                    for document in documents
                    if document["kind"] == "PodDisruptionBudget"
                ]
                self.assertIn(
                    {
                        "nvidia.com/dynamo-graph-deployment-name": deployment[
                            "metadata"
                        ]["name"],
                        "nvidia.com/dynamo-component": "Frontend",
                    },
                    disruption_budget_selectors,
                )
                self.assertIn(
                    {
                        "nvidia.com/dynamo-graph-deployment-name": deployment[
                            "metadata"
                        ]["name"],
                        "nvidia.com/dynamo-component": worker_name,
                    },
                    disruption_budget_selectors,
                )
                worker_script = services[worker_name]["extraPodSpec"][
                    "mainContainer"
                ]["args"][0]
                if worker_name == "VllmWorker":
                    max_num_seqs = re.search(r"--max-num-seqs (\d+)", worker_script)
                    self.assertIsNotNone(max_num_seqs)
                    self.assertGreaterEqual(int(max_num_seqs.group(1)), 3)
                else:
                    max_batch_size = re.search(
                        r"^max_batch_size: (\d+)$", worker_script, re.MULTILINE
                    )
                    self.assertIsNotNone(max_batch_size)
                    self.assertGreaterEqual(int(max_batch_size.group(1)), 3)

                if "Planner" in services:
                    planner_args = services["Planner"]["extraPodSpec"][
                        "mainContainer"
                    ]["args"]
                    planner_config = json.loads(
                        planner_args[planner_args.index("--config") + 1]
                    )
                    self.assertEqual(planner_config["min_endpoint"], 1)
                    self.assertEqual(planner_config["max_gpu_budget"], 2)

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

    def test_nemotron35lightning_l40s_uses_full_context_marlin_profile(
        self,
    ) -> None:
        documents = render(
            ROOT / "values" / "nemotron35lightning-l40s.yaml"
        )
        deployment = next(
            document
            for document in documents
            if document["kind"] == "DynamoGraphDeployment"
        )
        services = deployment["spec"]["services"]
        worker = services["VllmWorker"]
        main_container = worker["extraPodSpec"]["mainContainer"]
        worker_script = main_container["args"][0]

        self.assertEqual(deployment["spec"]["backendFramework"], "vllm")
        self.assertEqual(main_container["image"], NEMOTRON35_LIGHTNING_RUNTIME_IMAGE)
        self.assertEqual(worker["replicas"], 1)
        self.assertTrue(worker["scalingAdapter"]["enabled"])
        self.assertEqual(worker["resources"]["requests"]["gpu"], "1")
        self.assertEqual(
            worker["extraPodSpec"]["nodeSelector"],
            {"nvidia.com/gpu.product": "NVIDIA-L40S"},
        )
        self.assertIn("--tensor-parallel-size 1", worker_script)
        self.assertIn("--max-model-len 1048576", worker_script)
        self.assertIn("--max-num-seqs 4", worker_script)
        self.assertIn("--quantization modelopt_fp4", worker_script)
        self.assertIn("--moe-backend marlin", worker_script)
        self.assertIn("--mamba-ssm-cache-dtype float16", worker_script)

        planner_args = services["Planner"]["extraPodSpec"]["mainContainer"][
            "args"
        ]
        planner_config = json.loads(
            planner_args[planner_args.index("--config") + 1]
        )
        self.assertEqual(planner_config["min_endpoint"], 1)
        self.assertEqual(planner_config["max_gpu_budget"], 2)

    def test_glm47flash_uses_vllm_flags_and_model_context(self) -> None:
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
        self.assertNotIn("--max-model-len", worker_script)
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

    def test_poolside_uses_sanitized_managed_chat_template(self) -> None:
        documents = render(ROOT / "values" / "poolside21nvfp4.yaml")
        config_map = next(
            document
            for document in documents
            if document["kind"] == "ConfigMap"
            and document["metadata"]["name"]
            == "poolside21-vllm-agg-custom-jinja-template"
        )
        template = config_map["data"]["chat-template.jinja"]

        self.assertNotIn("{% generation %}", template)
        self.assertNotIn("{% endgeneration %}", template)
        self.assertNotIn("{%- generation -%}", template)
        self.assertNotIn("{%- endgeneration -%}", template)
        self.assertIn("{%- if add_generation_prompt -%}", template)
        self.assertIn("{{- \"<assistant>\" -}}", template)

        deployment = next(
            document
            for document in documents
            if document["kind"] == "DynamoGraphDeployment"
        )
        worker = deployment["spec"]["services"]["VllmWorker"]
        pod_spec = worker["extraPodSpec"]
        self.assertEqual(
            pod_spec["volumes"][0]["configMap"]["name"],
            "poolside21-vllm-agg-custom-jinja-template",
        )
        main_container = pod_spec["mainContainer"]
        self.assertEqual(
            main_container["volumeMounts"][0],
            {
                "name": "custom-jinja-template",
                "mountPath": "/etc/dynamo/laguna-chat-template.jinja",
                "subPath": "chat-template.jinja",
                "readOnly": True,
            },
        )
        worker_script = main_container["args"][0]
        self.assertIn("--custom-jinja-template \\", worker_script)
        self.assertIn(
            "/etc/dynamo/laguna-chat-template.jinja \\", worker_script
        )


if __name__ == "__main__":
    unittest.main()
