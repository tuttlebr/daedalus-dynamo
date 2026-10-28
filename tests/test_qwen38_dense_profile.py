from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CACHE = load_script("dense_profile_cache", "prepare_qwen36_fp8_cache.py")
with patch.dict(sys.modules, {"prepare_qwen36_fp8_cache": CACHE}):
    FRAMING = load_script("dense_profile_framing", "qwen36_fp8_postrender.py")
with patch.dict(sys.modules, {"qwen36_fp8_postrender": FRAMING}):
    POST = load_script("dense_profile_postrender", "qwen38_dense_postrender.py")


class DenseStagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raw = subprocess.check_output(
            ["helm", "template", "qwen38-27b-l40s", "charts/dynamo-model",
             "-n", "daedalus-dynamo", "-f", "values/qwen38-27b-l40s.yaml",
             "-f", "values/staging/qwen38-27b-bf16kv-l40s.yaml"],
            cwd=ROOT, text=True, timeout=20,
        )
        cls.baseline = [item for item in yaml.safe_load_all(raw) if item]
        cls.pin = json.loads(POST.PIN_PATH.read_text())

    def test_changes_only_frontend_and_worker_and_adds_framing(self):
        before = copy.deepcopy(self.baseline)
        after = POST.adapt(before, self.pin)
        self.assertEqual(before, self.baseline)
        before_graph = next(item for item in before if item["kind"] == "DynamoGraphDeployment")
        after_graph = next(item for item in after if item["kind"] == "DynamoGraphDeployment")
        for name in ["Frontend", "VllmWorker"]:
            before_graph["spec"]["services"][name] = after_graph["spec"]["services"][name]
        before_graph["spec"]["pvcs"].append(
            {"name": "qwen36-35b-fp8-l40s-model-cache", "create": False}
        )
        self.assertEqual(before, after[:-1])
        self.assertEqual(after[-1]["kind"], "ConfigMap")
        self.assertTrue(after[-1]["immutable"])
        for name in ["Frontend", "VllmWorker"]:
            service = after_graph["spec"]["services"][name]
            container = service["extraPodSpec"]["mainContainer"]
            environment = {item["name"]: item["value"] for item in container["env"]}
            self.assertEqual(environment["HF_HUB_OFFLINE"], "1")
            self.assertEqual(environment["TRANSFORMERS_OFFLINE"], "1")
            self.assertEqual(environment["DYN_HEALTH_CHECK_REQUEST_TIMEOUT"], "120")
            self.assertNotIn("DYN_HEALTH_CHECK_ENABLED", environment)
            self.assertIn(self.pin["revision"], container["args"][0])
        worker = after_graph["spec"]["services"]["VllmWorker"]
        script = worker["extraPodSpec"]["mainContainer"]["args"][0]
        self.assertEqual(worker["replicas"], 1)
        self.assertIn('--model "' + self.pin["snapshot"] + '"', script)
        self.assertNotIn("Waiting for ModelExpress", script)
        self.assertNotIn("--language-model-only", script)
        self.assertNotIn("--limit-mm-per-prompt", script)
        for argument in ["--kv-cache-dtype auto", "--dtype bfloat16", "--max-model-len 131072",
                         "--max-num-seqs 4", "--kv-cache-memory-bytes 9663676416"]:
            self.assertIn(argument, script)

    def test_refuses_different_graph_or_cache(self):
        for field, value in [("graph", "other-model"), ("cache", "other-cache")]:
            pin = {**self.pin, field: value}
            with self.assertRaises(ValueError):
                POST.adapt(self.baseline, pin)

    def test_startup_guard_rejects_metadata_corruption_and_missing_shard(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "model/snapshots/revision"
            snapshot.mkdir(parents=True)
            index = snapshot / "model.safetensors.index.json"
            index.write_text(json.dumps({"weight_map": {"weight": "weights.safetensors"}}))
            shard = snapshot / "weights.safetensors"
            shard.write_bytes(b"fixture")
            pin = {"snapshot": str(snapshot), "revision": "fixture",
                   "supporting_files": [{"file": index.name, "sha256": hashlib.sha256(index.read_bytes()).hexdigest()}],
                   "weight_files": [{"file": shard.name, "size": 7}]}
            def check():
                return subprocess.run(["bash", "-c", POST.startup_guard(pin)], capture_output=True, timeout=10)
            self.assertEqual(check().returncode, 0)
            index.write_text("{}")
            self.assertNotEqual(check().returncode, 0)
            index.write_text(json.dumps({"weight_map": {"weight": "weights.safetensors"}}))
            shard.unlink()
            self.assertNotEqual(check().returncode, 0)


if __name__ == "__main__":
    unittest.main()
