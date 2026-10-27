"""Small local cache fixtures only; no network, model or framework imports."""

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "super_cache_integrity",
    Path(__file__).resolve().parents[1]
    / "runtime/nemotron35super-gb10/cache_integrity.py",
)
ci = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci)


def git_blob(data):
    return hashlib.sha1(
        b"blob " + str(len(data)).encode() + b"\0" + data, usedforsecurity=False
    ).hexdigest()


class CacheIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.snapshot = self.root / ci.REPO_DIR / "snapshots" / ci.REVISION
        self.snapshot.mkdir(parents=True)
        index = {
            "weight_map": {
                "layer.a": "part-a.safetensors",
                "layer.b": "part-b.safetensors",
            }
        }
        self.content = {
            "config.json": b'{"model_type":"synthetic"}\n',
            "model.safetensors.index.json": json.dumps(index).encode(),
            "modeling_synthetic.py": b"SAFE = True\n",
            "tokenizer.json": b'{"a":1}\n',
            "part-a.safetensors": b"aaa",
            "part-b.safetensors": b"bbbb",
        }
        siblings = []
        for name, data in self.content.items():
            (self.snapshot / name).write_bytes(data)
            entry = {"rfilename": name, "size": len(data), "blob_id": git_blob(data)}
            if name.endswith(".safetensors"):
                entry["lfs"] = {
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size": len(data),
                }
            siblings.append(entry)
        self.info = {
            "id": ci.MODEL,
            "sha": ci.REVISION,
            "private": False,
            "gated": False,
            "siblings": siblings,
        }
        self.expect = {
            "expected_config": hashlib.sha256(self.content["config.json"]).hexdigest(),
            "expected_index": hashlib.sha256(
                self.content["model.safetensors.index.json"]
            ).hexdigest(),
            "expected_weight_count": 2,
            "expected_weight_bytes": 7,
        }

    def verified(self):
        return ci.verify_snapshot(
            self.snapshot, self.root, ci.metadata_files(self.info), **self.expect
        )

    def test_small_lfs_and_git_blob_positive_marker_and_offline_support(self):
        value = self.verified()
        ci.publish_marker(self.snapshot, value)
        raw = (self.snapshot / ci.MARKER).read_bytes()
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(json.loads(raw), value)
        self.assertEqual(
            ci.validate_offline(self.snapshot, self.root, **self.expect), value
        )
        self.assertEqual(
            value["verified_files"]["modeling_synthetic.py"]["algorithm"],
            "git_blob_sha1",
        )

    def test_metadata_rejects_wrong_pin_private_gated_unsafe_and_oversize(self):
        for patch in [
            {"sha": "0" * 40},
            {"id": "other/repo"},
            {"private": True},
            {"gated": "auto"},
        ]:
            with self.subTest(patch=patch), self.assertRaises(ci.IntegrityError):
                ci.metadata_files({**self.info, **patch})
        for name in ["../escape.py", "/absolute", "nested/../../escape", "x\\y"]:
            with self.subTest(name=name), self.assertRaises(ci.IntegrityError):
                ci.metadata_files(
                    {
                        **self.info,
                        "siblings": [
                            {"rfilename": name, "size": 1, "blob_id": "a" * 40}
                        ],
                    }
                )
        huge = {
            "rfilename": "huge.bin",
            "size": ci.MAX_DOWNLOAD_BYTES,
            "blob_id": "b" * 40,
        }
        with self.assertRaisesRegex(ci.IntegrityError, "footprint"):
            ci.metadata_files({**self.info, "siblings": self.info["siblings"] + [huge]})

    def test_git_blob_header_is_required_and_lfs_corruption_fails(self):
        files = ci.metadata_files(self.info)
        bad = {k: dict(v) for k, v in files.items()}
        bad["tokenizer.json"]["digest"] = hashlib.sha1(
            self.content["tokenizer.json"], usedforsecurity=False
        ).hexdigest()
        with self.assertRaisesRegex(ci.IntegrityError, "digest"):
            ci.verify_snapshot(self.snapshot, self.root, bad, **self.expect)
        (self.snapshot / "part-a.safetensors").write_bytes(b"zzz")
        with self.assertRaisesRegex(ci.IntegrityError, "digest"):
            ci.verify_snapshot(self.snapshot, self.root, files, **self.expect)
        self.assertFalse((self.snapshot / ci.MARKER).exists())

    def test_cached_remote_code_same_size_corruption_rejected_offline(self):
        ci.publish_marker(self.snapshot, self.verified())
        old = self.content["modeling_synthetic.py"]
        (self.snapshot / "modeling_synthetic.py").write_bytes(b"X" * len(old))
        with self.assertRaisesRegex(ci.IntegrityError, "digest"):
            ci.validate_offline(self.snapshot, self.root, **self.expect)

    def test_stale_marker_removed_before_failed_metadata_and_no_download(self):
        (self.snapshot / ci.MARKER).write_text('{"stale":true}\n')
        fake = types.ModuleType("huggingface_hub")
        download = mock.Mock(side_effect=AssertionError("must not fetch"))
        fake.snapshot_download = download
        api = mock.Mock()

        def metadata(*args, **kwargs):
            self.assertFalse((self.snapshot / ci.MARKER).exists())
            return {**self.info, "sha": "wrong"}

        api.model_info.side_effect = metadata
        fake.HfApi = mock.Mock(return_value=api)
        with (
            mock.patch.dict(sys.modules, {"huggingface_hub": fake}),
            mock.patch.object(ci, "CACHE_ROOT", self.root),
        ):
            with self.assertRaises(ci.IntegrityError):
                ci.download_main()
        download.assert_not_called()
        fake.HfApi.assert_called_once_with(
            endpoint="https://huggingface.co", token=False
        )
        self.assertFalse((self.snapshot / ci.MARKER).exists())

    def test_oversized_metadata_prevents_snapshot_fetch(self):
        fake = types.ModuleType("huggingface_hub")
        fake.snapshot_download = mock.Mock(side_effect=AssertionError("must not fetch"))
        huge = {
            "rfilename": "huge.bin",
            "size": ci.MAX_DOWNLOAD_BYTES,
            "blob_id": "b" * 40,
        }
        fake.HfApi = mock.Mock(
            return_value=types.SimpleNamespace(
                model_info=lambda *a, **k: {
                    **self.info,
                    "siblings": self.info["siblings"] + [huge],
                }
            )
        )
        with (
            mock.patch.dict(sys.modules, {"huggingface_hub": fake}),
            mock.patch.object(ci, "CACHE_ROOT", self.root),
        ):
            with self.assertRaisesRegex(ci.IntegrityError, "footprint"):
                ci.download_main()
        fake.snapshot_download.assert_not_called()

    def test_file_fsync_failure_has_no_marker_or_temporary(self):
        with mock.patch.object(
            ci.os, "fsync", side_effect=OSError("synthetic fsync failure")
        ):
            with self.assertRaises(OSError):
                ci.publish_marker(self.snapshot, self.verified())
        self.assertFalse((self.snapshot / ci.MARKER).exists())
        self.assertEqual(list(self.snapshot.glob(".stage-verified.*.tmp")), [])

    def test_atomic_replace_and_directory_fsync_failure_removes_marker(self):
        value = self.verified()
        seen = []
        original_replace = ci.os.replace

        def replace(source, destination):
            self.assertFalse(Path(destination).exists())
            self.assertEqual(json.loads(Path(source).read_bytes()), value)
            seen.append("replace")
            return original_replace(source, destination)

        with (
            mock.patch.object(ci.os, "replace", side_effect=replace),
            mock.patch.object(
                ci,
                "fsync_directory",
                side_effect=OSError("synthetic directory fsync failure"),
            ),
        ):
            with self.assertRaises(OSError):
                ci.publish_marker(self.snapshot, value)
        self.assertEqual(seen, ["replace"])
        self.assertFalse((self.snapshot / ci.MARKER).exists())
        self.assertEqual(list(self.snapshot.glob(".stage-verified.*.tmp")), [])

    def test_unexpected_support_and_escaping_symlink_rejected(self):
        files = ci.metadata_files(self.info)
        (self.snapshot / "unexpected.py").write_text("no")
        with self.assertRaisesRegex(ci.IntegrityError, "unexpected"):
            ci.verify_snapshot(self.snapshot, self.root, files, **self.expect)
        (self.snapshot / "unexpected.py").unlink()
        with tempfile.NamedTemporaryFile() as outside:
            path = self.snapshot / "modeling_synthetic.py"
            path.unlink()
            path.symlink_to(outside.name)
            with self.assertRaisesRegex(ci.IntegrityError, "escapes"):
                ci.verify_snapshot(self.snapshot, self.root, files, **self.expect)


if __name__ == "__main__":
    unittest.main(verbosity=2)
