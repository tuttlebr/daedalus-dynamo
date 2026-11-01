from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from model_values import helm_values, list_recipes, resolve, values_path  # noqa: E402
from build_model_catalog import generate  # noqa: E402
from simulate_model_catalog import prediction  # noqa: E402


class RecipePortabilityTests(unittest.TestCase):
    def test_context_policy_and_muse_tuning_survive_catalog_regeneration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "config/model-catalog", root / "config/model-catalog")
            shutil.copytree(ROOT / "values", root / "values")
            catalog = generate(root)
            for path in list_recipes(root / "values"):
                with self.subTest(path=path):
                    values = resolve(path)
                    if not values["recipe"].get("upstream"):
                        self.assertEqual(values["worker"]["vllmOptions"]["max-model-len"], "auto")
            model = root / "values/meta/muse-glimmer-30b-nvfp4"
            values = resolve(model / "dgx-spark/tp1-pp1-dp1.yaml")
            self.assertEqual(values["recipe"]["qualification"], "requires-auto-context-gpu-validation")
            self.assertEqual(values["recipe"]["validation"]["maxModelLen"], 32768)
            options = values["worker"]["vllmOptions"]
            self.assertEqual(options["max-model-len"], "auto")
            # The failed startup observed 99.31 GiB free out of 121.63 GiB.
            # Leave at least 10 GiB for other unified-memory allocations.
            executor_gib = 121.63 * options["gpu-memory-utilization"]
            self.assertLess(executor_gib, 99.31 - 10)
            self.assertGreaterEqual(int(values["worker"]["memoryRequest"].removesuffix("Gi")), executor_gib)
            rendered = subprocess.check_output([
                "helm", "template", "muse-test", "charts/dynamo-model", "-f",
                values_path(model / "dgx-spark/tp1-pp1-dp1.yaml")], cwd=ROOT, text=True)
            graph = next(d for d in yaml.safe_load_all(rendered) if d and d["kind"] == "DynamoGraphDeployment")
            worker = graph["spec"]["services"]["VllmWorker"]
            self.assertEqual(worker["annotations"]["nvidia.com/deployment-rolling-update-max-surge"], "0")
            self.assertEqual(worker["annotations"]["nvidia.com/deployment-rolling-update-max-unavailable"], "1")
            command = worker["extraPodSpec"]["mainContainer"]["args"][0]
            self.assertIn("--max-model-len 'auto'", command)
            frontend_command = graph["spec"]["services"]["Frontend"]["extraPodSpec"]["mainContainer"]["args"][0]
            self.assertIn("--dyn-chat-processor", frontend_command)
            self.assertIn("muse_glimmer", frontend_command)
            muse = next(m for m in catalog["models"] if m["id"] == "nvidia/Muse-Glimmer-30B-NVFP4")
            assessment = next(a for a in muse["assessments"] if a["hardware"] == "dgx-spark")
            self.assertEqual(assessment["context_length"], 32768)
            self.assertLess(assessment["estimated_gib_per_rank"], assessment["budget_gib_per_rank"])
            l40s = resolve(model / "l40s/tp1-pp1-dp1.yaml")["worker"]["vllmOptions"]
            self.assertEqual(l40s["gpu-memory-utilization"], 0.9)
            self.assertEqual(l40s["max-model-len"], "auto")
            self.assertEqual(resolve(model / "l40s/tp1-pp1-dp1.yaml")["frontend"]["extraArgs"], values["frontend"]["extraArgs"])

    def test_simulation_uses_numeric_screening_context_for_auto_serving(self) -> None:
        for path, screening_context in (
            ("values/qwen/qwen3-8b-nvfp4/l40s/tp1-pp1-dp1.yaml", 8192),
            ("values/google/gemma-4-31b-it-nvfp4/l40s/tp1-pp1-dp1.yaml", 4096),
            ("values/meta/muse-glimmer-30b-nvfp4/dgx-spark/tp1-pp1-dp1.yaml", 32768),
        ):
            values = resolve(ROOT / path)
            for setting in ("auto", -1, 2048):
                with self.subTest(path=path, setting=setting):
                    values["worker"]["vllmOptions"]["max-model-len"] = setting
                    result = prediction({"id": values["model"]["id"]}, values, "/model")
                    expected = 2048 if setting == 2048 else screening_context
                    self.assertEqual(result["engine"]["context_length"], expected)
                    traffic = result["traffic"]["source"]
                    self.assertLessEqual(traffic["input_tokens"] + traffic["output_tokens"], expected)
                    self.assertEqual(values["worker"]["vllmOptions"]["max-model-len"], setting)

    def test_layer_precedence_lists_and_cycle_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "base.yaml").write_text("worker:\n  gpu: '1'\n  extraEnv: [old]\n  vllmOptions: {max-model-len: 8192, enforce-eager: true}\n")
            (root / "recipe.yaml").write_text("extends: [base.yaml]\nworker:\n  gpu: '2'\n  extraEnv: [new]\n  vllmOptions: {max-model-len: 4096}\n")
            worker = resolve(root / "recipe.yaml")["worker"]
            self.assertEqual(worker["gpu"], "2")
            self.assertEqual(worker["extraEnv"], ["new"])
            self.assertEqual(worker["vllmOptions"], {"max-model-len": 4096, "enforce-eager": True})
            (root / "base.yaml").write_text("extends: [recipe.yaml]\n")
            with self.assertRaisesRegex(ValueError, "inheritance cycle"):
                resolve(root / "recipe.yaml")

    def test_collection_coverage_single_node_limits_and_revision_pins(self) -> None:
        snapshot = json.loads((ROOT / "config/model-catalog/snapshot.json").read_text())
        catalog = json.loads((ROOT / "config/model-catalog/recipes.json").read_text())
        self.assertEqual({m["id"] for m in snapshot["models"]}, {m["id"] for m in catalog["models"]})
        names = set()
        for model in catalog["models"]:
            self.assertTrue(model["recipes"] or model.get("reason"), model["id"])
            if model["status"] != "candidate-recipes":
                continue
            for path in model["recipes"]:
                with self.subTest(path=path):
                    values = resolve(ROOT / path)
                    recipe = values["recipe"]
                    name = values["model"]["name"]
                    self.assertNotIn(name, names)
                    names.add(name)
                    self.assertLessEqual(len(name + "-modelexpress"), 63)
                    self.assertEqual(recipe["release"], name)
                    tp = recipe["parallelism"]["tp"]
                    self.assertLessEqual(tp, 2 if recipe["hardware"] == "l40s" else 1)
                    self.assertEqual(values["worker"]["gpu"], str(tp))
                    self.assertEqual(values["worker"]["replicas"], 1)
                    self.assertFalse(values["planner"]["enabled"])
                    self.assertFalse(values["worker"]["scalingAdapter"]["enabled"])
                    options = values["worker"]["vllmOptions"]
                    if not recipe.get("upstream"):
                        self.assertEqual(options["max-model-len"], "auto")
                    assessment = next(a for a in model["assessments"]
                                      if a["hardware"] == recipe["hardware"] and a["tp"] == tp)
                    self.assertEqual(recipe["screeningContextLength"], assessment["context_length"])
                    self.assertEqual(options["pipeline-parallel-size"], 1)
                    self.assertEqual(options["data-parallel-size"], 1)
                    self.assertEqual(options["tensor-parallel-size"], tp)
                    self.assertEqual(options["revision"], model["revision"])
                    self.assertEqual(values["modelexpress"]["modelRevision"], model["revision"])
                    self.assertRegex(model["revision"], r"^[0-9a-f]{40}$")
                    self.assertNotIn("recipe", helm_values(values))
                    if "NVFP4" in model["id"] and not recipe.get("upstream"):
                        self.assertEqual(options["linear-backend"], "marlin")

    def test_retired_profiles_are_only_regression_fixtures(self) -> None:
        self.assertFalse(list((ROOT / "values").glob("*.yaml")))
        self.assertFalse((ROOT / "values/staging").exists())
        fixture = ROOT / "tests/fixtures/retired-values/qwen27b.yaml"
        self.assertEqual(values_path(fixture), str(fixture))
        listed = subprocess.check_output(["make", "models"], cwd=ROOT, text=True)
        self.assertNotIn("retired-values", listed)
        self.assertNotIn("model.yaml", listed)
        self.assertIn("values/qwen/qwen3-8b-nvfp4/l40s/tp1-pp1-dp1.yaml", listed)

    def test_shared_configmap_and_all_hardware_render(self) -> None:
        shared = subprocess.check_output(["helm", "template", "runtime", "charts/dynamo-model-runtime"], cwd=ROOT, text=True)
        configmap = yaml.safe_load(shared)
        self.assertTrue(configmap["immutable"])
        self.assertEqual(set(configmap["data"]), {"server.sh", "wait-cache.sh"})
        for hardware, tp in (("dgx-spark", 1), ("l40s", 1), ("l40s", 2), ("a30", 1)):
            path = ROOT / f"values/qwen/qwen3-8b-nvfp4/{hardware}/tp{tp}-pp1-dp1.yaml"
            values = resolve(path)
            command = ["helm", "template", values["recipe"]["release"], "charts/dynamo-model", "-f", values_path(path)]
            documents = list(yaml.safe_load_all(subprocess.check_output(command, cwd=ROOT, text=True)))
            self.assertFalse(any(d and d["kind"] == "ConfigMap" for d in documents))
            graph = next(d for d in documents if d and d["kind"] == "DynamoGraphDeployment")
            for component, script in (("ModelExpress", "server.sh"), ("VllmWorker", "wait-cache.sh")):
                pod = graph["spec"]["services"][component]["extraPodSpec"]
                self.assertEqual(pod["volumes"][0]["configMap"]["name"], configmap["metadata"]["name"])
                container = pod["mainContainer"]
                self.assertIn("/opt/modelexpress-runtime/" + script, container["args"][0])
                if component == "VllmWorker":
                    self.assertIn("--max-model-len 'auto'", container["args"][0])
                environment = {e["name"]: e["value"] for e in container["env"]}
                self.assertEqual(environment["MODEL_REVISION"], values["modelexpress"]["modelRevision"])
                subprocess.run(["bash", "-n"], input=container["args"][0], text=True, check=True)

    def test_make_resolves_unique_release_and_keeps_new_recipes_private(self) -> None:
        path = "values/qwen/qwen3-8b-nvfp4/l40s/tp2-pp1-dp1.yaml"
        result = subprocess.check_output(["make", "-n", "deploy", path, "SKIP_SECRET=1"], cwd=ROOT, text=True)
        values = resolve(ROOT / path)
        self.assertIn(f'helm upgrade --install "{values["recipe"]["release"]}"', result)
        self.assertIn('if [[ "0" =~', result)
        self.assertIn('/resolved-values/', result)
        route = subprocess.check_output(["make", "-n", "gateway-route-apply", path], cwd=ROOT, text=True)
        self.assertIn(f'MODEL_ROUTE_NAME="daedalus-dynamo-{values["recipe"]["release"]}"', route)


if __name__ == "__main__":
    unittest.main()
