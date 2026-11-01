from __future__ import annotations

import copy
import ast
import hashlib
import importlib.util
import json
import math
import shlex
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from build_model_catalog import generate  # noqa: E402
from model_values import helm_values, resolve  # noqa: E402
from simulate_model_catalog import simulate  # noqa: E402


def load_runtime_module(name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "runtime/nvidia-spark" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def flags(command: str) -> dict:
    tokens = shlex.split(command.replace("\\\n", " "))
    result = {}
    i = 0
    while i < len(tokens):
        if tokens[i].startswith("--"):
            key = tokens[i][2:]
            value = True
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                i += 1
                try:
                    value = json.loads(tokens[i])
                except ValueError:
                    value = tokens[i]
            result[key] = value
        i += 1
    return result


class NvidiaSparkRecipeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.lock = json.loads((ROOT / "config/model-catalog/nvidia-spark.json").read_text())

    def test_all_published_options_reach_the_correct_dynamo_component(self) -> None:
        self.assertEqual(len(self.lock["profiles"]), 12)
        for profile in self.lock["profiles"]:
            with self.subTest(model=profile["model"]), tempfile.TemporaryDirectory() as directory:
                values = resolve(ROOT / profile["values"])
                path = Path(directory) / "values.yaml"
                path.write_text(yaml.safe_dump(helm_values(values)))
                rendered = subprocess.check_output([
                    "helm", "template", values["recipe"]["release"], "charts/dynamo-model", "-f", str(path),
                ], cwd=ROOT, text=True)
                graph = next(d for d in yaml.safe_load_all(rendered)
                             if d and d["kind"] == "DynamoGraphDeployment")
                services = graph["spec"]["services"]
                worker = services["VllmWorker"]["extraPodSpec"]["mainContainer"]
                frontend = services["Frontend"]["extraPodSpec"]["mainContainer"]
                worker_script = worker["args"][0]
                frontend_script = frontend["args"][0]
                engine = flags(worker_script.rsplit("exec python3 -m dynamo.vllm", 1)[1])
                chat = flags(frontend_script.rsplit("exec python3 -m dynamo.frontend", 1)[1])
                for key, expected in profile["options"].items():
                    if key in ("host", "port"):
                        self.assertNotIn(key, engine)
                        continue
                    if key in ("enable-auto-tool-choice", "tool-call-parser", "default-chat-template-kwargs"):
                        self.assertEqual(chat[key], expected)
                        self.assertNotIn(key, engine)
                    else:
                        if key == "speculative-config" and profile["draft"]:
                            expected = copy.deepcopy(expected)
                            draft = profile["draft"]
                            expected["model"] = (values["modelCache"].get("cachePath", "/model/.model-express/cache")
                                                 + "/models--" + draft["id"].replace("/", "--")
                                                 + "/snapshots/" + draft["revision"])
                        self.assertEqual(engine[key], expected, key)
                self.assertEqual(chat["reasoning-parser"], profile["options"]["reasoning-parser"])
                self.assertEqual(chat["dyn-chat-processor"], "vllm")
                self.assertNotIn("enforce-eager", engine)
                self.assertNotIn("language-model-only", engine)
                # An inherited Marlin default must not override nvfp4_dynamic.
                if "linear-backend" not in profile["options"]:
                    self.assertNotIn("linear-backend", engine)
                for key in ("max-model-len", "max-num-seqs", "max-num-batched-tokens", "kv-cache-dtype"):
                    if key not in profile["options"]:
                        self.assertNotIn(key, engine)
                self.assertEqual(engine["revision"], profile["revision"])
                self.assertEqual(values["modelexpress"]["modelRevision"], profile["revision"])
                self.assertEqual(worker["image"], frontend["image"])
                environment = {e["name"]: e["value"] for e in worker["env"]}
                for key, value in profile["environment"].items():
                    self.assertEqual(environment[key], value)
                if profile["draft"]:
                    self.assertIn("prefetch_models.py", worker_script)
                    self.assertLess(worker_script.index("prefetch_models.py"), worker_script.index("exec python3"))
                    self.assertEqual(values["worker"]["prefetchModels"], [
                        {k: profile["draft"][k] for k in ("id", "revision")},
                    ])
                for script in (worker_script, frontend_script):
                    subprocess.run(["bash", "-n"], input=script, text=True, check=True)

    def test_regeneration_preserves_profiles_resources_and_other_hardware(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "values", root / "values")
            shutil.copytree(ROOT / "config/model-catalog", root / "config/model-catalog")
            before = {p: resolve(root / p) for p in [x["values"] for x in self.lock["profiles"]]}
            other = {str(p.relative_to(root)): p.read_bytes() for p in (root / "values").rglob("*.yaml")
                     if "/l40s/" in str(p) or "/a30/" in str(p)}
            manifest = generate(root)
            self.assertEqual(len(manifest["supplemental_recipes"]), 2)
            for profile in self.lock["profiles"]:
                path = profile["values"]
                with self.subTest(path=path):
                    values = resolve(root / path)
                    self.assertEqual(values, before[path])
                    worker = values["worker"]
                    self.assertEqual(worker["gpu"], "1")
                    self.assertEqual(worker["replicas"], 1)
                    self.assertEqual(values["recipe"]["parallelism"], {"tp": 1, "pp": 1, "dp": 1})
                    self.assertEqual(worker["nodeSelector"], {
                        "kubernetes.io/arch": "arm64", "nvidia.com/gpu.product": "NVIDIA-GB10",
                    })
                    budget = 121.52756881713867 * profile["options"]["gpu-memory-utilization"]
                    self.assertGreaterEqual(int(worker["memoryRequest"].removesuffix("Gi")), math.ceil(budget) + 4)
                    self.assertEqual(worker["annotations"]["nvidia.com/deployment-rolling-update-max-surge"], "0")
                    self.assertTrue(values["recipe"]["buildRequired"])
                    self.assertEqual(values["recipe"]["qualification"], "requires-image-build-and-gpu-validation")
                    self.assertFalse(values["planner"]["enabled"])
                    self.assertFalse(worker["scalingAdapter"]["enabled"])
                    if profile["draft"]:
                        capacity = int(values["modelCache"]["storage"].removesuffix("Gi")) * 2**30
                        self.assertGreater(capacity, 1.2 * (profile["weight_bytes"] + profile["draft"]["weight_bytes"]))
            for path, content in other.items():
                self.assertEqual((root / path).read_bytes(), content, path)

    def test_image_builds_are_pinned_and_preserve_native_dynamo(self) -> None:
        for name, runtime in self.lock["runtimes"].items():
            dockerfile = (ROOT / "runtime/nvidia-spark" / f"{name}.Dockerfile").read_text()
            self.assertRegex(runtime["base_image"], r"@sha256:[a-f0-9]{64}$")
            self.assertIn("FROM --platform=${SPARK_PLATFORM} " + runtime["base_image"], dockerfile)
            self.assertIn("FROM --platform=${SPARK_PLATFORM} " + self.lock["native_runtime_image"], dockerfile)
            self.assertIn("COPY --from=dynamo-native", dockerfile)
            self.assertIn(f"verify_runtime.py --runtime {name}", dockerfile)
        path = "values/qwen/qwen3-8-27b-nvfp4/dgx-spark/tp1-pp1-dp1.yaml"
        build = subprocess.check_output(["make", "-n", "build", path], cwd=ROOT, text=True)
        self.assertIn("ngc-2610.Dockerfile", build)
        self.assertIn("linux/arm64", build)
        self.assertNotIn("kubectl", build)

    def test_flash_next_uses_separate_local_cache_and_bounded_security_changes(self) -> None:
        profile = next(p for p in self.lock["profiles"] if "Flash-Next" in p["model"])
        values = resolve(ROOT / profile["values"])
        self.assertFalse(values["recipe"]["gateway"])
        self.assertEqual(values["modelCache"]["storageClassName"], "local-fast")
        self.assertEqual(values["modelCache"]["accessModes"], ["ReadWriteOnce"])
        for role in ("frontend", "modelexpress", "worker"):
            self.assertEqual(values[role]["nodeSelector"]["nvidia.com/gpu.product"], "NVIDIA-GB10")
        worker = values["worker"]
        self.assertEqual(worker["securityContext"]["seccompProfile"]["type"], "Unconfined")
        self.assertNotIn("privileged", worker["securityContext"])
        mounts = worker["extraVolumeMounts"]
        self.assertEqual(len(mounts), 4)
        self.assertTrue(all(m["name"] == values["modelCache"]["name"] for m in mounts))
        self.assertNotIn("/root/.cache", [m["mountPath"] for m in mounts])
        legacy = resolve(ROOT / "values/qwen/qwen3-8-flash-next-nvfp4/dgx-spark/tp1-pp1-dp1-nvme-offload.yaml")
        self.assertNotEqual(legacy["model"]["id"], values["model"]["id"])
        self.assertNotEqual(legacy["modelCache"]["name"], values["modelCache"]["name"])

    def test_old_simulation_is_not_reused_for_new_runtime(self) -> None:
        profile = self.lock["profiles"][0]
        values = resolve(ROOT / profile["values"])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            target = output / values["recipe"]["release"]
            target.mkdir()
            (target / "outcome.json").write_text('{"status":"predicted-estimate"}')
            with patch("simulate_model_catalog.subprocess.run") as run:
                result = simulate({"id": profile["model"], "revision": profile["revision"]},
                                  profile["values"], output, "unused", 1)
            self.assertEqual(result["status"], "requires-model-specific-simulator-support")
            run.assert_not_called()


class DraftPrefetchTests(unittest.TestCase):
    def test_pinned_downloads_use_shared_cache_and_reject_wrong_snapshot(self) -> None:
        module = load_runtime_module("prefetch_models")
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            model = module.PinnedModel("vendor/draft", "a" * 40)
            snapshot = model.snapshot(cache)
            snapshot.mkdir(parents=True)
            fake = types.ModuleType("huggingface_hub")
            with patch.dict(sys.modules, {"huggingface_hub": fake}):
                with patch.object(fake, "snapshot_download", create=True, return_value=str(snapshot)) as download:
                    module.prefetch([model], cache)
                    download.assert_called_once_with(repo_id="vendor/draft", revision="a" * 40, cache_dir=str(cache))
                with patch.object(fake, "snapshot_download", create=True, return_value=str(cache)):
                    with self.assertRaisesRegex(RuntimeError, "Unexpected draft snapshot"):
                        module.prefetch([model], cache)
        with self.assertRaises(ValueError):
            module.PinnedModel("vendor/draft", "main")
        with self.assertRaises(ValueError):
            module.PinnedModel("../../draft", "a" * 40)

    def test_embedding_adapter_rejects_unknown_source(self) -> None:
        module = load_runtime_module("install_dynamo")
        with self.assertRaisesRegex(ValueError, "Unexpected Dynamo embedding helper"):
            module.defer_embedding_import("unexpected source")

    def test_embedding_adapter_changes_only_the_embedding_import_scope(self) -> None:
        module = load_runtime_module("install_dynamo")
        source = (
            "from vllm.v1.engine.utils import get_engine_zmq_addresses, launch_core_engines\n"
            "def create_shared_embedding_engine_client():\n"
            "    '''Embedding-only client.'''\n"
            "    return get_engine_zmq_addresses()\n"
            "def generation():\n"
            "    return launch_core_engines()\n"
        )
        with patch.object(module, "EMBEDDING_SHA256", hashlib.sha256(source.encode()).hexdigest()):
            adapted = ast.parse(module.defer_embedding_import(source))
        self.assertEqual([n.name for n in adapted.body[0].names], ["launch_core_engines"])
        self.assertIsInstance(adapted.body[1].body[1], ast.ImportFrom)
        self.assertEqual(adapted.body[1].body[1].names[0].name, "get_engine_zmq_addresses")
        self.assertEqual(ast.dump(adapted.body[2]), ast.dump(ast.parse(source).body[2]))

    def test_native_binding_export_keeps_metadata_and_fails_on_missing_libraries(self) -> None:
        module = load_runtime_module("export_native_runtime")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            site = root / "installed"
            core = site / "dynamo/_core.abi3.so"
            metadata = site / "ai_dynamo_runtime-1.6.1.dist-info/METADATA"
            for path in (core, metadata):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"native-donor-fixture")
            package = types.SimpleNamespace(version="1.6.1", files=[p.relative_to(site) for p in (core, metadata)],
                                            locate_file=lambda item: site / item)
            with patch.object(module, "distribution", return_value=package), \
                    patch.object(module.platform, "machine", return_value="aarch64"):
                with patch.object(module.subprocess, "check_output", return_value="libc.so.6 => /lib/libc.so.6 (0x0)"):
                    module.export(root / "export")
                self.assertEqual((root / "export/site-packages/dynamo/_core.abi3.so").read_bytes(), core.read_bytes())
                self.assertTrue((root / "export/site-packages" / metadata.relative_to(site)).exists())
                manifest = json.loads((root / "export/manifest.json").read_text())
                self.assertEqual(manifest["core_sha256"], hashlib.sha256(core.read_bytes()).hexdigest())
                with patch.object(module.subprocess, "check_output", return_value="libavutil_dynamo.so => not found"):
                    with self.assertRaises(RuntimeError):
                        module.export(root / "bad-export")


if __name__ == "__main__":
    unittest.main()
