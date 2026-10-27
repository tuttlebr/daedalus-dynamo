from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "qwen36_cache", ROOT / "scripts/prepare_qwen36_fp8_cache.py"
)
CACHE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CACHE)


class PinnedCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name) / "hf"
        self.pin = CACHE.load_pin(CACHE.PIN_PATH)
        self.pin["marker"] = str(Path(self.temp.name) / "verified.json")
        self.snapshot = (
            self.cache
            / "models--Qwen--Qwen3.6-35B-A3B-FP8"
            / "snapshots"
            / self.pin["revision"]
        )
        self.snapshot.mkdir(parents=True)
        siblings = []
        for index, item in enumerate(self.pin["weight_files"]):
            data = f"synthetic shard {index}".encode()
            item["size"] = len(data)
            (self.snapshot / item["file"]).write_bytes(data)
            siblings.append(
                {
                    "rfilename": item["file"],
                    "size": len(data),
                    "lfs": {"sha256": hashlib.sha256(data).hexdigest()},
                }
            )
        metadata = {
            "config.json": b'{"synthetic":true}',
            "model.safetensors.index.json": json.dumps(
                {
                    "weight_map": {
                        str(i): f["file"]
                        for i, f in enumerate(self.pin["weight_files"])
                    }
                }
            ).encode(),
            "tokenizer.json": b'{"synthetic_tokenizer":true}',
            "tokenizer_config.json": b'{"synthetic_configuration":true}',
            "chat_template.jinja": b"synthetic template",
            "generation_config.json": b'{"synthetic_generation":true}',
        }
        for name, data in metadata.items():
            (self.snapshot / name).write_bytes(data)
            siblings.append(
                {
                    "rfilename": name,
                    "size": len(data),
                    "lfs": None,
                    "blob_id": hashlib.sha1(
                        f"blob {len(data)}\0".encode() + data, usedforsecurity=False
                    ).hexdigest(),
                }
            )
        self.pin["config_sha256"] = hashlib.sha256(metadata["config.json"]).hexdigest()
        self.pin["index_sha256"] = hashlib.sha256(
            metadata["model.safetensors.index.json"]
        ).hexdigest()
        self.pin["supporting_files"] = [
            {"file": name, "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in metadata.items()
        ]
        self.metadata = {"sha": self.pin["revision"], "siblings": siblings}

    def test_manifest_is_pinned_to_separate_claim_and_paths(self):
        original = CACHE.load_pin(CACHE.PIN_PATH)
        for key, value in [
            ("revision", "main"),
            ("new_cache", original["old_cache"]),
            ("snapshot", "/model/old-model"),
            ("marker", "/model/old-marker"),
        ]:
            with self.subTest(key=key):
                pin = {**original, key: value}
                path = Path(self.temp.name) / "pin.json"
                path.write_text(json.dumps(pin))
                with self.assertRaises(ValueError):
                    CACHE.load_pin(path)

    def test_all_weights_and_tokenizer_metadata_are_verified(self):
        proof = CACHE.verify_snapshot(self.pin, self.cache, self.metadata)
        self.assertEqual(len(proof["weight_sha256"]), 42)
        self.assertTrue(proof["weights_lfs_sha256_verified"])
        self.assertTrue(proof["tokenizer_and_metadata_verified"])
        self.assertIn("chat_template.jinja", proof["metadata_sha256"])
        self.assertEqual(proof["revision"], CACHE.REVISION)

    def test_equal_size_shard_corruption_fails(self):
        path = self.snapshot / self.pin["weight_files"][0]["file"]
        path.write_bytes(b"x" * path.stat().st_size)
        with self.assertRaisesRegex(ValueError, "shard checksum"):
            CACHE.verify_snapshot(self.pin, self.cache, self.metadata)

    def test_tokenizer_corruption_and_missing_metadata_fail(self):
        path = self.snapshot / "chat_template.jinja"
        path.write_bytes(b"x" * path.stat().st_size)
        with self.assertRaisesRegex(ValueError, "supporting-file checksum"):
            CACHE.verify_snapshot(self.pin, self.cache, self.metadata)
        path.write_bytes(b"synthetic template")
        self.metadata["siblings"] = [
            x
            for x in self.metadata["siblings"]
            if x["rfilename"] != "chat_template.jinja"
        ]
        with self.assertRaisesRegex(ValueError, "Tokenizer metadata"):
            CACHE.verify_snapshot(self.pin, self.cache, self.metadata)

    def test_wrong_revision_and_incomplete_lfs_metadata_fail(self):
        with self.assertRaisesRegex(ValueError, "revision"):
            CACHE.verify_snapshot(
                self.pin, self.cache, {**self.metadata, "sha": "other"}
            )
        self.metadata["siblings"][0]["lfs"] = None
        with self.assertRaisesRegex(ValueError, "LFS metadata"):
            CACHE.verify_snapshot(self.pin, self.cache, self.metadata)

    def test_symlink_escape_is_rejected(self):
        path = self.snapshot / self.pin["weight_files"][0]["file"]
        outside = Path(self.temp.name) / "outside"
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "escapes"):
            CACHE.verify_snapshot(self.pin, self.cache, self.metadata)

    def test_prepare_uses_exact_hf_revision_and_publishes_only_after_success(self):
        api = types.SimpleNamespace(model_info=lambda *a, **kw: self.metadata)
        fake_hub = types.SimpleNamespace(HfApi=lambda **kw: api)
        with patch.dict("sys.modules", {"huggingface_hub": fake_hub}), patch.object(
            CACHE.subprocess, "run"
        ) as run:
            CACHE.prepare(self.pin, self.cache)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], ["hf", "download", CACHE.MODEL])
        self.assertEqual(argv[argv.index("--revision") + 1], CACHE.REVISION)
        self.assertNotIn("--token", argv)
        marker = Path(self.pin["marker"])
        self.assertTrue(
            json.loads(marker.read_text())["tokenizer_and_metadata_verified"]
        )
        # A subsequent bad check must remove the old success proof.
        (self.snapshot / "config.json").write_text("corrupt")
        with patch.dict("sys.modules", {"huggingface_hub": fake_hub}), patch.object(
            CACHE.subprocess, "run"
        ), self.assertRaises(ValueError):
            CACHE.prepare(self.pin, self.cache)
        self.assertFalse(marker.exists())

    def test_atomic_marker_failure_never_replaces_existing_proof(self):
        marker = Path(self.pin["marker"])
        marker.write_text("prior proof")
        with patch.object(CACHE.os, "replace", side_effect=OSError("synthetic")):
            with self.assertRaises(OSError):
                CACHE.write_marker(marker, {"new": "proof"})
        self.assertEqual(marker.read_text(), "prior proof")
        self.assertEqual(list(marker.parent.glob(marker.name + ".*")), [])

    def test_failed_directory_sync_removes_new_marker(self):
        marker = Path(self.pin["marker"])
        with patch.object(CACHE.os, "fsync", side_effect=[None, OSError("synthetic")]):
            with self.assertRaises(OSError):
                CACHE.write_marker(marker, {"new": "proof"})
        self.assertFalse(marker.exists())

    def test_render_only_job_is_cpu_only_and_uses_new_retained_claim(self):
        pin = CACHE.load_pin(CACHE.PIN_PATH)
        with patch.object(
            CACHE.subprocess, "run", side_effect=AssertionError("no execution")
        ):
            config, job = CACHE.render_job(
                pin, "fp8-cache-95a723d0-001", "daedalus-dynamo"
            )
        pod = job["spec"]["template"]["spec"]
        container = pod["containers"][0]
        self.assertEqual(pod["nodeSelector"], {"kubernetes.io/hostname": "daedalus-03"})
        self.assertEqual(container["image"], CACHE.IMAGE)
        self.assertEqual(container["imagePullPolicy"], "IfNotPresent")
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertNotIn("nvidia.com/gpu", json.dumps(container["resources"]))
        env = {x["name"]: x for x in container["env"]}
        self.assertEqual(env["NVIDIA_VISIBLE_DEVICES"]["value"], "void")
        self.assertEqual(env["CUDA_VISIBLE_DEVICES"]["value"], "")
        self.assertNotIn("value", env["HF_TOKEN"])
        self.assertEqual(
            pod["volumes"][0]["persistentVolumeClaim"]["claimName"], pin["new_cache"]
        )
        self.assertEqual(json.loads(config["data"]["model-pin.json"]), pin)
        pvc = yaml.safe_load(
            (ROOT / "runtime/qwen36-35b-fp8-l40s/cache-pvc.yaml").read_text()
        )
        self.assertEqual(pvc["metadata"]["name"], pin["new_cache"])
        self.assertEqual(
            pvc["metadata"]["annotations"]["helm.sh/resource-policy"], "keep"
        )
        self.assertEqual(pvc["spec"]["resources"]["requests"]["storage"], "64Gi")
        self.assertEqual(pvc["spec"]["accessModes"], ["ReadWriteMany"])


if __name__ == "__main__":
    unittest.main()
