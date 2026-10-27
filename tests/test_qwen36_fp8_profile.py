from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import resource
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "charts/dynamo-model"
BASE_VALUES = ROOT / "values/qwen38-27b-l40s.yaml"
CANDIDATE_VALUES = ROOT / "values/staging/qwen36-35b-fp8-l40s.yaml"
RELEASE = "qwen38-27b-l40s"
REVISION = "95a723d08a9490559dae23d0cff1d9466213d989"
IMAGE = (
    "nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.4.1@sha256:"
    "a6bb45ad652f01d08b5a18b54b9fa9e80584ce18ee1ba1e5a760a8f228872fe5"
)


def load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CACHE = load_script("fp8_profile_cache", "prepare_qwen36_fp8_cache.py")
with patch.dict(sys.modules, {"prepare_qwen36_fp8_cache": CACHE}):
    POST = load_script("fp8_profile_postrender", "qwen36_fp8_postrender.py")


def render(values: Path) -> list[dict]:
    completed = subprocess.run(
        [
            "helm",
            "template",
            RELEASE,
            str(CHART),
            "--namespace",
            "daedalus-dynamo",
            "--values",
            str(values),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    return [item for item in yaml.safe_load_all(completed.stdout) if item]


def graph(objects: list[dict]) -> dict:
    graphs = [item for item in objects if item["kind"] == "DynamoGraphDeployment"]
    if len(graphs) != 1:
        raise AssertionError("Expected exactly one graph deployment")
    return graphs[0]


def objects_by_identity(objects: list[dict]) -> dict:
    return {
        (item["apiVersion"], item["kind"], item["metadata"]["name"]): item
        for item in objects
    }


def container(service: dict) -> dict:
    return service["extraPodSpec"]["mainContainer"]


def environment(service: dict) -> dict[str, str]:
    return {item["name"]: item["value"] for item in container(service)["env"]}


def arguments_after(argv: list[str], flag: str) -> list[str]:
    start = argv.index(flag) + 1
    end = next(
        (i for i in range(start, len(argv)) if argv[i].startswith("--")), len(argv)
    )
    return argv[start:end]


class OfficialFp8ProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.production_pin = CACHE.load_pin(CACHE.PIN_PATH)
        cls.baseline = render(BASE_VALUES)
        cls.candidate = render(CANDIDATE_VALUES)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qwen fp8 profile ")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.snapshot = self.directory / "hf/models--synthetic/snapshots" / REVISION
        self.snapshot.mkdir(parents=True)
        self.pin = copy.deepcopy(self.production_pin)
        self.pin["snapshot"] = str(self.snapshot)
        self.pin["marker"] = str(self.directory / "verified.json")
        weights = {"part-a.safetensors": b"synthetic-a", "part-b.safetensors": b"b"}
        support = {
            "config.json": b'{"synthetic":true}',
            "model.safetensors.index.json": json.dumps(
                {"weight_map": {"a": "part-a.safetensors", "b": "part-b.safetensors"}}
            ).encode(),
            "tokenizer.json": b'{"synthetic_tokenizer":true}',
            "tokenizer_config.json": b'{"synthetic_configuration":true}',
            "generation_config.json": b'{"synthetic_generation":true}',
            "chat_template.jinja": b"synthetic template",
        }
        for name, data in {**weights, **support}.items():
            (self.snapshot / name).write_bytes(data)
        self.pin["weight_files"] = [
            {"file": name, "size": len(data)} for name, data in weights.items()
        ]
        self.pin["supporting_files"] = [
            {"file": name, "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in support.items()
        ]
        self.pin["config_sha256"] = hashlib.sha256(support["config.json"]).hexdigest()
        self.pin["index_sha256"] = hashlib.sha256(
            support["model.safetensors.index.json"]
        ).hexdigest()
        self.marker = {
            "schema_version": 1,
            "model": self.pin["candidate_model"],
            "revision": REVISION,
            "pin_manifest_sha256": CACHE.manifest_digest(self.pin),
            "weights_lfs_sha256_verified": True,
            "weight_sha256": {
                name: hashlib.sha256(data).hexdigest() for name, data in weights.items()
            },
            "tokenizer_and_metadata_verified": True,
            "metadata_sha256": {
                name: hashlib.sha256(data).hexdigest() for name, data in support.items()
            },
        }
        self.write_marker(self.marker)
        package = self.directory / "python/dynamo"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        fake_module = (
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "Path(os.environ['CAPTURE_PATH']).write_text(json.dumps({\n"
            " 'argv':sys.argv[1:], 'module':__file__,\n"
            " 'offline':{k:os.environ.get(k) for k in [\n"
            " 'HF_HUB_OFFLINE','TRANSFORMERS_OFFLINE','HF_HUB_DISABLE_IMPLICIT_TOKEN']}\n"
            "}))\n"
        )
        for name in ["frontend.py", "vllm.py"]:
            (package / name).write_text(fake_module)

    def write_marker(self, marker: dict):
        Path(self.pin["marker"]).write_text(json.dumps(marker))

    def run_startup(
        self, component: str, extra_env: dict[str, str] | None = None
    ) -> tuple[subprocess.CompletedProcess, Path]:
        services = graph(POST.adapt(self.candidate, self.pin))["spec"]["services"]
        service = services[component]
        capture = self.directory / f"{component}-argv.json"
        capture.unlink(missing_ok=True)
        env = {
            "PATH": os.defpath,
            "PYTHONPATH": str(self.directory / "python"),
            "CAPTURE_PATH": str(capture),
            "CUDA_VISIBLE_DEVICES": "",
            "NVIDIA_VISIBLE_DEVICES": "void",
            **environment(service),
        }
        # Exercise the emitted Bash unchanged without raising host descriptor limits.
        hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
        nofile = min(4096, hard_limit) if hard_limit != resource.RLIM_INFINITY else 4096
        key = (
            "DYN_WORKER_NOFILE_LIMIT"
            if component == "VllmWorker"
            else "DYN_FRONTEND_NOFILE_LIMIT"
        )
        env[key] = str(nofile)
        env.update(extra_env or {})
        completed = subprocess.run(
            container(service)["command"] + container(service)["args"],
            env=env,
            cwd=self.directory,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return completed, capture

    def assert_blocked(self, extra_env: dict[str, str] | None = None):
        for component in ["Frontend", "VllmWorker"]:
            with self.subTest(component=component):
                completed, capture = self.run_startup(component, extra_env)
                self.assertNotEqual(completed.returncode, 0)
                self.assertFalse(
                    capture.exists(), "Model module ran before verification"
                )

    def test_helm_objects_and_unrelated_consumers_are_preserved(self):
        before = copy.deepcopy(self.candidate)
        adapted = POST.adapt(self.candidate, self.production_pin)
        self.assertEqual(self.candidate, before)
        baseline = objects_by_identity(self.baseline)
        candidate = objects_by_identity(adapted)
        self.assertEqual(set(candidate), set(baseline))
        for identity, item in baseline.items():
            if identity[1] != "DynamoGraphDeployment":
                self.assertEqual(candidate[identity], item, identity)
        old, new = graph(self.baseline), graph(adapted)
        self.assertEqual(old["metadata"], new["metadata"])
        old_spec, new_spec = copy.deepcopy(old["spec"]), copy.deepcopy(new["spec"])
        old_services, new_services = old_spec.pop("services"), new_spec.pop("services")
        old_pvcs, new_pvcs = old_spec.pop("pvcs"), new_spec.pop("pvcs")
        self.assertEqual(new_spec, old_spec)
        self.assertEqual(
            new_pvcs, old_pvcs + [{"name": self.pin["new_cache"], "create": False}]
        )
        self.assertEqual(set(new_services), set(old_services))
        new_consumers = []
        for name, service in new_services.items():
            if name not in ["Frontend", "VllmWorker"]:
                self.assertEqual(service, old_services[name], name)
                continue
            self.assertEqual(
                service["volumeMounts"],
                [{"name": self.pin["new_cache"], "mountPoint": "/model"}],
            )
            new_consumers.append(name)
            old_service, new_service = (
                copy.deepcopy(old_services[name]),
                copy.deepcopy(service),
            )
            for item in [old_service, new_service]:
                item.pop("volumeMounts")
                main = container(item)
                main.pop("args")
                main.pop("env")
            self.assertEqual(new_service, old_service, name)
        self.assertEqual(new_consumers, ["Frontend", "VllmWorker"])
        self.assertEqual(new_services["ModelExpress"], old_services["ModelExpress"])

    def test_offline_environment_is_confined_to_frontend_and_worker(self):
        services = graph(POST.adapt(self.candidate, self.production_pin))["spec"][
            "services"
        ]
        baseline = graph(self.baseline)["spec"]["services"]
        for name in ["Frontend", "VllmWorker"]:
            env = environment(services[name])
            original = environment(baseline[name])
            self.assertEqual(container(services[name])["image"], IMAGE)
            self.assertEqual(env["HF_HUB_CACHE"], "/model/hf")
            for key in [
                "HF_HUB_OFFLINE",
                "TRANSFORMERS_OFFLINE",
                "HF_HUB_DISABLE_IMPLICIT_TOKEN",
            ]:
                self.assertEqual(env[key], "1")
            changed = {
                key
                for key in set(env) | set(original)
                if env.get(key) != original.get(key)
            }
            permitted = {
                "HF_HUB_CACHE",
                "HF_HUB_OFFLINE",
                "TRANSFORMERS_OFFLINE",
                "HF_HUB_DISABLE_IMPLICIT_TOKEN",
            }
            if name == "VllmWorker":
                permitted |= {"MODEL_NAME", "MODEL_CACHE_PATH"}
                permitted |= {
                    key for key in original if key.startswith("MODEL_EXPRESS_")
                }
                self.assertFalse(any(key.startswith("MODEL_EXPRESS_") for key in env))
                self.assertEqual(env["MODEL_NAME"], self.pin["candidate_model"])
                self.assertEqual(env["MODEL_CACHE_PATH"], "/model/hf")
            self.assertEqual(changed, permitted)

    def test_emitted_worker_bash_preserves_exact_pins_aliases_and_bounds(self):
        completed, capture = self.run_startup("VllmWorker")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        actual = json.loads(capture.read_text())
        self.assertEqual(
            Path(actual["module"]), self.directory / "python/dynamo/vllm.py"
        )
        argv = actual["argv"]
        expected = {
            "--model": str(self.snapshot),
            "--tokenizer": str(self.snapshot),
            "--load-format": "safetensors",
            "--revision": REVISION,
            "--tokenizer-revision": REVISION,
            "--quantization": "fp8",
            "--moe-backend": "triton",
            "--tensor-parallel-size": "1",
            "--pipeline-parallel-size": "1",
            "--data-parallel-size": "1",
            "--max-model-len": "131072",
            "--max-num-seqs": "3",
            "--max-num-batched-tokens": "4096",
            "--kv-cache-memory-bytes": "4294967296",
            "--kv-cache-dtype": "fp8",
            "--prefix-caching-hash-algo": "sha256",
            "--reasoning-parser": "qwen3",
            "--dyn-tool-call-parser": "qwen3_coder",
            "--dyn-reasoning-parser": "qwen3",
            "--gpu-memory-utilization": "0.90",
            "--scheduling-policy": "priority",
        }
        for flag, value in expected.items():
            self.assertEqual(argv.count(flag), 1, flag)
            self.assertEqual(arguments_after(argv, flag), [value], flag)
        self.assertEqual(
            arguments_after(argv, "--served-model-name"),
            ["Qwen/Qwen3.6-35B-A3B-FP8", "Qwen/Qwen3.8-27B-FP8"],
        )
        for flag in [
            "--enforce-eager",
            "--enable-chunked-prefill",
            "--enable-prefix-caching",
            "--language-model-only",
            "--frontend-decoding",
            "--dyn-enable-structural-tag",
            "--no-enable-log-requests",
        ]:
            self.assertEqual(argv.count(flag), 1, flag)
        for flag in ["--trust-remote-code", "--speculative-config", "--cpu-offload-gb"]:
            self.assertNotIn(flag, argv)
        self.assertEqual(set(actual["offline"].values()), {"1"})

    def test_emitted_frontend_bash_uses_guard_before_existing_parser_entrypoint(self):
        completed, capture = self.run_startup("Frontend")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        actual = json.loads(capture.read_text())
        self.assertEqual(
            Path(actual["module"]), self.directory / "python/dynamo/frontend.py"
        )
        self.assertEqual(
            arguments_after(actual["argv"], "--dyn-chat-processor"), ["vllm"]
        )
        self.assertEqual(
            arguments_after(actual["argv"], "--tool-call-parser"), ["qwen3_coder"]
        )
        self.assertEqual(
            arguments_after(actual["argv"], "--reasoning-parser"), ["qwen3"]
        )
        self.assertEqual(set(actual["offline"].values()), {"1"})

    def test_missing_or_invalid_marker_blocks_both_entrypoints(self):
        Path(self.pin["marker"]).unlink()
        self.assert_blocked()
        for field, wrong in [
            ("schema_version", 0),
            ("model", "other"),
            ("revision", "main"),
            ("pin_manifest_sha256", "0" * 64),
            ("weights_lfs_sha256_verified", False),
            ("tokenizer_and_metadata_verified", False),
        ]:
            with self.subTest(field=field):
                self.write_marker({**self.marker, field: wrong})
                self.assert_blocked()
        Path(self.pin["marker"]).write_text("not json")
        self.assert_blocked()

    def test_supporting_file_corruption_or_missing_file_blocks_execution(self):
        for name in [item["file"] for item in self.pin["supporting_files"]]:
            with self.subTest(file=name):
                path = self.snapshot / name
                original = path.read_bytes()
                path.write_bytes(b"x" * len(original))
                self.assert_blocked()
                path.unlink()
                self.assert_blocked()
                path.write_bytes(original)

    def test_optimized_python_environment_cannot_disable_marker_checks(self):
        self.write_marker({**self.marker, "tokenizer_and_metadata_verified": False})
        self.assert_blocked({"PYTHONOPTIMIZE": "1"})

    def test_metadata_hash_attestation_and_weight_inventory_fail_closed(self):
        for changes in [
            {
                "metadata_sha256": {
                    **self.marker["metadata_sha256"],
                    "tokenizer.json": "0" * 64,
                }
            },
            {"metadata_sha256": {}},
            {"weight_sha256": {}},
            {"weight_sha256": {**self.marker["weight_sha256"], "unexpected": "0" * 64}},
            {
                "weight_sha256": {
                    name: "invalid" for name in self.marker["weight_sha256"]
                }
            },
        ]:
            with self.subTest(changes=list(changes)):
                self.write_marker({**self.marker, **changes})
                self.assert_blocked()
        self.write_marker(self.marker)
        path = self.snapshot / self.pin["weight_files"][0]["file"]
        path.write_bytes(b"wrong size")
        self.assert_blocked()
        path.unlink()
        self.assert_blocked()

    def test_cache_blob_links_work_but_symlink_escape_is_rejected(self):
        name = self.pin["weight_files"][0]["file"]
        path = self.snapshot / name
        data = path.read_bytes()
        blob = self.snapshot.parents[2] / "blobs/inside"
        blob.parent.mkdir()
        blob.write_bytes(data)
        path.unlink()
        path.symlink_to(blob)
        completed, capture = self.run_startup("VllmWorker")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(capture.exists())
        for filename in [name, "tokenizer.json"]:
            with self.subTest(file=filename):
                item = self.snapshot / filename
                original = item.read_bytes()
                outside = self.directory / "outside"
                outside.write_bytes(original)
                item.unlink()
                item.symlink_to(outside)
                self.assert_blocked()
                item.unlink()
                item.write_bytes(original)

    def test_adaptation_rejects_wrong_graph_duplicate_patch_and_unexpected_mount(self):
        cases = []
        wrong_graph = copy.deepcopy(self.candidate)
        graph(wrong_graph)["metadata"]["name"] = "another-consumer"
        cases.append(wrong_graph)
        cases.append(POST.adapt(self.candidate, self.production_pin))
        wrong_mount = copy.deepcopy(self.candidate)
        graph(wrong_mount)["spec"]["services"]["Frontend"]["volumeMounts"][0][
            "name"
        ] = "another-cache"
        cases.append(wrong_mount)
        for objects in cases:
            with self.subTest(graph=graph(objects)["metadata"]["name"]):
                before = copy.deepcopy(objects)
                with self.assertRaises(ValueError):
                    POST.adapt(objects, self.production_pin)
                self.assertEqual(objects, before)


if __name__ == "__main__":
    unittest.main()
