from __future__ import annotations

import copy
import json
import subprocess
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "charts/dynamo-model"
BOLT = ROOT / "values/nemotron35bolt-gb10.yaml"
PROFILES = sorted(
    path
    for path in (ROOT / "values").rglob("*.yaml")
    if "model" in (yaml.safe_load(path.read_text()) or {})
)


def render(profile: Path, override=None):
    command = [
        "helm",
        "template",
        "probe-fixture",
        str(CHART),
        "--namespace",
        "daedalus-dynamo",
        "--values",
        str(profile),
    ]
    if override is not None:
        command.extend(
            ["--set-json", "worker.livenessFailureThreshold=" + json.dumps(override)]
        )
    return subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, timeout=30, check=False
    )


def objects(completed):
    if completed.returncode:
        raise AssertionError(completed.stderr)
    return [item for item in yaml.safe_load_all(completed.stdout) if item]


def worker(documents, profile):
    graph = next(item for item in documents if item["kind"] == "DynamoGraphDeployment")
    values = yaml.safe_load(profile.read_text())
    component = values.get("worker", {}).get("componentName", "VllmWorker")
    return graph["spec"]["services"][component]


class WorkerLivenessProbeTests(unittest.TestCase):
    def test_only_bolt_extends_failure_window_and_all_other_fields_are_preserved(self):
        self.assertTrue(PROFILES)
        self.assertIn(BOLT, PROFILES)
        for profile in PROFILES:
            with self.subTest(profile=str(profile.relative_to(ROOT))):
                proposed = objects(render(profile))
                original_threshold = objects(render(profile, 6))
                component = worker(proposed, profile)
                self.assertEqual(
                    component["livenessProbe"],
                    {
                        "httpGet": {"path": "/live", "port": "system"},
                        "periodSeconds": 10,
                        "timeoutSeconds": 5,
                        "failureThreshold": 30 if profile == BOLT else 6,
                    },
                )
                self.assertEqual(
                    component["readinessProbe"],
                    worker(original_threshold, profile)["readinessProbe"],
                )
                restored = copy.deepcopy(proposed)
                worker(restored, profile)["livenessProbe"]["failureThreshold"] = 6
                self.assertEqual(restored, original_threshold)

    def test_invalid_failure_thresholds_are_rejected_before_rendering(self):
        for value in (0, -1, False, True, 1.5, "30", [], {}):
            with self.subTest(value=value):
                completed = render(BOLT, value)
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("livenessFailureThreshold", completed.stderr)


if __name__ == "__main__":
    unittest.main()
