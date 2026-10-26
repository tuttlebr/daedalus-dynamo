from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
QWEN_CONFIG = ROOT / "values" / "qwen27b.yaml"
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
        completed = run_make("-n", "render", "values/qwen27b.yaml")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn('helm template "qwen27b"', completed.stdout)
        self.assertIn('-f "values/qwen27b.yaml"', completed.stdout)
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
            "values/qwen27b.yaml",
            "SKIP_BUILD=1",
            "SKIP_PUSH=1",
            "SKIP_SECRET=1",
            "GATEWAY=0",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(
            'make --no-print-directory upgrade "values/qwen27b.yaml"',
            completed.stdout,
        )
        self.assertIn('helm upgrade --install "qwen27b"', completed.stdout)
        self.assertIn('-f "values/qwen27b.yaml"', completed.stdout)

    def test_route_name_can_be_retained_for_a_renamed_config(self) -> None:
        completed = run_make(
            "-n",
            "gateway-route-apply",
            "values/qwen27b.yaml",
            "MODEL_ROUTE_NAME=retained-route",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn('MODEL_ROUTE_NAME="retained-route"', completed.stdout)
        self.assertIn(f'MODEL_ID="{QWEN_MODEL_ID}"', completed.stdout)

    def test_models_lists_checked_in_config_paths(self) -> None:
        completed = run_make("models")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("values/qwen27b.yaml", completed.stdout.splitlines())
        self.assertNotIn("qwen27b", completed.stdout.splitlines())

    def test_spark_build_selects_model_dockerfile_context_and_platform(self) -> None:
        completed = run_make("-n", "build", "values/qwen38-flash-next-gb10.yaml")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn('-f "runtime/qwen38-flash-next-gb10/Dockerfile"', completed.stdout)
        self.assertIn('"runtime/qwen38-flash-next-gb10"', completed.stdout)
        self.assertIn('--platform "linux/arm64"', completed.stdout)
        overridden = run_make("-n", "build", "values/qwen38-flash-next-gb10.yaml",
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
                "render", "values/qwen27b.yaml", "values/qwen35b.yaml"
            ),
            "Usage: make render <model-config.yaml>",
        )

    def test_config_path_must_exist_and_have_a_yaml_extension(self) -> None:
        self.assert_failed_with(
            run_make("render", "README.md"),
            "Model config must end in .yaml or .yml",
        )
        self.assert_failed_with(
            run_make("render", "values/does-not-exist.yaml"),
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
