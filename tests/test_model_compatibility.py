from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "model_compatibility", ROOT / "scripts/model_compatibility.py"
)
COMPAT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(COMPAT)


def render_route(model: str) -> dict:
    result = subprocess.run(
        ["scripts/render-inference-gateway.sh", "route"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "MODEL_ID": model,
            "MODEL_DGD": "retired-qwen-worker",
            "MODEL_ROUTE_NAME": "retained-client-route",
        },
    )
    return yaml.safe_load(result.stdout)


class SingleModelCompatibilityTests(unittest.TestCase):
    def test_known_aliases_preserve_request_identity_and_replay_to_bolt(self):
        policy = COMPAT.load_policy()
        for alias in policy["aliases"]:
            route = render_route(alias)
            self.assertEqual(route["metadata"]["name"], "retained-client-route")
            self.assertEqual(
                route["spec"]["rules"][0]["matches"],
                [
                    {
                        "path": {"type": "PathPrefix", "value": "/v1/"},
                        "headers": [
                            {
                                "type": "Exact",
                                "name": "X-Gateway-Base-Model-Name",
                                "value": alias,
                            }
                        ],
                    }
                ],
            )
            self.assertEqual(
                route["spec"]["rules"][0]["backendRefs"],
                [{"name": "nemotron35bolt-vllm-gb10-frontend", "port": 8000}],
            )
            self.assertEqual(route["spec"]["rules"][0]["timeouts"], {"request": "300s"})
            annotations = route["metadata"]["annotations"]
            self.assertEqual(annotations["daedalus-dynamo/model-id"], alias)
            self.assertEqual(
                COMPAT.catalog_metadata(alias, annotations, policy),
                {
                    "compatibility_alias": True,
                    "canonical_model": COMPAT.CANONICAL_MODEL,
                    "model_revision": policy["canonical_revision"],
                },
            )

    def test_catalog_retains_legacy_ids_and_discloses_the_actual_model(self):
        routes = [render_route(alias) for alias in sorted(COMPAT.ALIASES)]
        completed = subprocess.run(
            [sys.executable, "scripts/http-routes-to-models-json.py"],
            cwd=ROOT,
            input=json.dumps({"items": routes}),
            capture_output=True,
            text=True,
            check=True,
        )
        models = json.loads(completed.stdout)["data"]
        self.assertEqual({model["id"] for model in models}, COMPAT.ALIASES)
        self.assertTrue(
            all(model["canonical_model"] == COMPAT.CANONICAL_MODEL for model in models)
        )
        self.assertTrue(all(model["compatibility_alias"] is True for model in models))

    def test_absolute_route_renderer_preserves_compatibility_outside_repository(self):
        alias = sorted(COMPAT.ALIASES)[0]
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [str(ROOT / "scripts/render-inference-gateway.sh"), "route"],
                cwd=directory,
                check=True,
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "MODEL_ID": alias,
                    "MODEL_DGD": "retired-qwen-worker",
                    "MODEL_ROUTE_NAME": "retained-client-route",
                },
            )
        route = yaml.safe_load(result.stdout)
        self.assertEqual(
            route["spec"]["rules"][0]["backendRefs"][0]["name"],
            "nemotron35bolt-vllm-gb10-frontend",
        )

    def test_other_model_routes_do_not_gain_aliases_or_change_backends(self):
        route = render_route("unrelated/model")
        self.assertEqual(
            route["spec"]["rules"][0]["backendRefs"][0]["name"],
            "retired-qwen-worker-frontend",
        )
        self.assertNotIn(
            "daedalus-dynamo/compatibility-alias", route["metadata"]["annotations"]
        )
        self.assertEqual(
            COMPAT.catalog_metadata(
                "unrelated/model",
                route["metadata"]["annotations"],
                COMPAT.load_policy(),
            ),
            {},
        )

    def test_invalid_policy_cannot_redirect_to_unknown_models_or_cycles(self):
        original = COMPAT.load_policy()
        changes = [
            {"canonical_model": "unknown/model"},
            {"backend_dgd": "unknown-backend"},
            {"canonical_revision": "latest"},
            {"canonical_revision": "A" * 40},
            {"aliases": [COMPAT.CANONICAL_MODEL, "Qwen/Qwen3.8-27B-FP8"]},
            {"aliases": ["Qwen/Qwen3.8-27B-FP8", "Qwen/Qwen3.8-27B-FP8"]},
            {"aliases": ["unknown/model", "Qwen/Qwen3.8-27B-FP8"]},
            {"fallback": "cloud"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            for change in changes:
                policy = {**original, **change}
                path.write_text(json.dumps(policy))
                with self.assertRaises(ValueError):
                    COMPAT.load_policy(path)

    def test_catalog_rejects_false_or_incomplete_alias_annotations(self):
        policy = COMPAT.load_policy()
        alias = sorted(COMPAT.ALIASES)[0]
        original = render_route(alias)["metadata"]["annotations"]
        for name in [
            "daedalus-dynamo/canonical-model-id",
            "daedalus-dynamo/canonical-model-revision",
            "daedalus-dynamo/model-dgd",
        ]:
            broken = {**original, name: "unknown"}
            with self.assertRaises(ValueError):
                COMPAT.catalog_metadata(alias, broken, policy)
        with self.assertRaises(ValueError):
            COMPAT.catalog_metadata(alias, {}, policy)
        with self.assertRaises(ValueError):
            COMPAT.catalog_metadata("unknown/model", original, policy)

    def test_one_worker_and_aliases_match_the_pinned_bolt_weights(self):
        policy = COMPAT.load_policy()
        bolt = yaml.safe_load((ROOT / "tests/fixtures/retired-values/nemotron35bolt-gb10.yaml").read_text())
        argv = shlex.split(" ".join(bolt["worker"]["vllmArgs"]))
        start = argv.index("--served-model-name") + 1
        end = next(
            index for index in range(start, len(argv)) if argv[index].startswith("--")
        )
        self.assertEqual(argv[start:end], ["${MODEL_NAME}", *policy["aliases"]])
        self.assertEqual(bolt["model"]["id"], policy["canonical_model"])
        self.assertEqual(
            argv[argv.index("--revision") + 1], policy["canonical_revision"]
        )
        self.assertEqual(
            bolt["modelexpress"]["modelRevision"], policy["canonical_revision"]
        )
        self.assertEqual(bolt["worker"]["replicas"], 1)
        self.assertEqual(bolt["worker"]["gpu"], "1")
        for name in [
            "tests/fixtures/retired-values/qwen38-27b-l40s.yaml",
            "tests/fixtures/retired-values/staging/qwen36-35b-fp8-l40s.yaml",
        ]:
            qwen = yaml.safe_load((ROOT / name).read_text())
            self.assertEqual(qwen["worker"]["replicas"], 0)
            self.assertFalse(qwen["worker"]["scalingAdapter"]["enabled"])
            self.assertEqual(qwen["frontend"]["replicas"], 1)
            self.assertEqual(qwen["modelCache"]["name"], "qwen38-27b-l40s-model-cache")
            self.assertTrue(qwen["modelCache"]["create"])
            self.assertTrue(qwen["redis"]["enabled"])


if __name__ == "__main__":
    unittest.main()
