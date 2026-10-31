from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
QWEN_CONFIG = ROOT / "tests/fixtures/retired-values" / "qwen27b.yaml"
QWEN_MODEL_ID = yaml.safe_load(QWEN_CONFIG.read_text(encoding="utf-8"))["model"]["id"]


def run_make(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["make", "--no-print-directory", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


class MakeCliTests(unittest.TestCase):
    def assert_failed_with(
        self, completed: subprocess.CompletedProcess[str], message: str
    ) -> None:
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(message, completed.stdout + completed.stderr)

    def test_render_uses_positional_config_and_filename_stem(self) -> None:
        completed = run_make("-n", "render", "tests/fixtures/retired-values/qwen27b.yaml")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn('helm template "qwen27b"', completed.stdout)
        self.assertIn('-f "tests/fixtures/retired-values/qwen27b.yaml"', completed.stdout)
        self.assertNotIn("Nothing to be done", completed.stdout + completed.stderr)

    def test_absolute_yml_path_and_release_override_are_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "copied-model.yml"
            shutil.copyfile(QWEN_CONFIG, config)

            completed = run_make(
                "-n", "render", str(config), "RELEASE=retained-release"
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn('helm template "retained-release"', completed.stdout)
        self.assertIn(f'-f "{config}"', completed.stdout)

    def test_deploy_dry_run_propagates_the_config_to_recursive_make(self) -> None:
        completed = run_make(
            "-n",
            "deploy",
            "tests/fixtures/retired-values/qwen27b.yaml",
            "SKIP_BUILD=1",
            "SKIP_PUSH=1",
            "SKIP_SECRET=1",
            "GATEWAY=0",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(
            'make --no-print-directory upgrade "tests/fixtures/retired-values/qwen27b.yaml"',
            completed.stdout,
        )
        self.assertIn('helm upgrade --install "qwen27b"', completed.stdout)
        self.assertIn('-f "tests/fixtures/retired-values/qwen27b.yaml"', completed.stdout)

    def test_route_name_can_be_retained_for_a_renamed_config(self) -> None:
        completed = run_make(
            "-n",
            "gateway-route-apply",
            "tests/fixtures/retired-values/qwen27b.yaml",
            "MODEL_ROUTE_NAME=retained-route",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn('MODEL_ROUTE_NAME="retained-route"', completed.stdout)
        self.assertIn(f'MODEL_ID="{QWEN_MODEL_ID}"', completed.stdout)

    def test_recipe_can_enable_gateway_and_caller_can_override(self) -> None:
        path = "values/meta/muse-glimmer-30b-nvfp4/dgx-spark/tp1-pp1-dp1.yaml"
        enabled = run_make("-n", "deploy", path, "SKIP_SECRET=1")
        self.assertEqual(enabled.returncode, 0, enabled.stderr)
        self.assertIn('if [[ "true" =~', enabled.stdout)
        self.assertIn(f'gateway-route-apply "{path}"', enabled.stdout)
        disabled = run_make("-n", "deploy", path, "SKIP_SECRET=1", "GATEWAY=0")
        self.assertEqual(disabled.returncode, 0, disabled.stderr)
        self.assertIn('if [[ "0" =~', disabled.stdout)

    def test_all_model_install_paths_default_to_shared_fixed_gateway(self) -> None:
        path = "values/meta/muse-glimmer-30b-nvfp4/l40s/tp1-pp1-dp1.yaml"
        for action in ("deploy", "install", "upgrade"):
            with self.subTest(action=action):
                result = run_make("-n", action, path, "SKIP_SECRET=1")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('if [[ "1" =~', result.stdout)
                self.assertIn('GATEWAY_PORT="32360"', result.stdout)
                self.assertIn('GATEWAY_NODE_PORT="32360"', result.stdout)
                self.assertIn(f'gateway-route-apply "{path}"', result.stdout)
                self.assertIn('scripts/render-inference-gateway.sh route', result.stdout)
                self.assertIn('scripts/render-inference-gateway.sh catalog', result.stdout)
                model_install = result.stdout.index('helm upgrade --install "mx-muse-')
                self.assertLess(result.stdout.index('scripts/render-inference-gateway.sh base'),
                                model_install)
                self.assertLess(model_install,
                                result.stdout.index('scripts/render-inference-gateway.sh route'))
        disabled = run_make("-n", "deploy", path, "SKIP_SECRET=1", "GATEWAY=0")
        self.assertEqual(disabled.returncode, 0, disabled.stderr)
        self.assertNotIn('scripts/render-inference-gateway.sh', disabled.stdout)

    def test_make_rejects_public_port_drift(self) -> None:
        for variable in ("GATEWAY_PORT", "GATEWAY_NODE_PORT"):
            with self.subTest(variable=variable):
                self.assert_failed_with(
                    run_make("-n", "gateway-base", f"{variable}=32359"),
                    "must both remain 32360",
                )

    def test_models_lists_checked_in_config_paths(self) -> None:
        completed = run_make("models")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("values/qwen/qwen3-8b-nvfp4/l40s/tp1-pp1-dp1.yaml", completed.stdout.splitlines())
        self.assertNotIn("qwen27b", completed.stdout.splitlines())

    def test_spark_build_selects_model_dockerfile_context_and_platform(self) -> None:
        completed = run_make("-n", "build", "tests/fixtures/retired-values/qwen38-flash-next-gb10.yaml")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn('-f "runtime/qwen38-flash-next-gb10/Dockerfile"', completed.stdout)
        self.assertIn('"runtime/qwen38-flash-next-gb10"', completed.stdout)
        self.assertIn('--platform "linux/arm64"', completed.stdout)
        overridden = run_make("-n", "build", "tests/fixtures/retired-values/qwen38-flash-next-gb10.yaml",
                              "DOCKERFILE=custom.Dockerfile", "CONTEXT=custom-context",
                              "BUILD_PLATFORM=linux/amd64")
        self.assertEqual(overridden.returncode, 0, overridden.stderr)
        self.assertIn('-f "custom.Dockerfile" "custom-context"', overridden.stdout)
        self.assertIn('--platform "linux/amd64"', overridden.stdout)

    def test_model_action_requires_exactly_one_config_goal(self) -> None:
        self.assert_failed_with(
            run_make("render"), "Usage: make render <model-config.yaml>"
        )
        self.assert_failed_with(
            run_make(
                "render", "tests/fixtures/retired-values/qwen27b.yaml", "tests/fixtures/retired-values/qwen35b.yaml"
            ),
            "Usage: make render <model-config.yaml>",
        )

    def test_config_path_must_exist_and_have_a_yaml_extension(self) -> None:
        self.assert_failed_with(
            run_make("render", "README.md"),
            "Model config must end in .yaml or .yml",
        )
        self.assert_failed_with(
            run_make("render", "tests/fixtures/retired-values/does-not-exist.yaml"),
            "Model config does not exist",
        )

    def test_required_model_fields_are_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "incomplete.yaml"
            config.write_text("model:\n  name: incomplete\n", encoding="utf-8")

            completed = run_make("check-model", str(config))

        self.assert_failed_with(completed, "could not read model.image")

    def test_legacy_model_selectors_and_targets_are_rejected(self) -> None:
        self.assert_failed_with(
            run_make("render", "MODEL=qwen27b"),
            "MODEL and MODEL_VALUES are no longer supported",
        )
        self.assert_failed_with(
            run_make("-n", "render-qwen27b"),
            "No rule to make target 'render-qwen27b'",
        )
        for target in ("lint-all", "deploy-all", "uninstall-all"):
            with self.subTest(target=target):
                self.assert_failed_with(
                    run_make("-n", target),
                    f"No rule to make target '{target}'",
                )


if __name__ == "__main__":
    unittest.main()
