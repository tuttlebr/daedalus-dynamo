"""CPU-only render regression for the native Dynamo health-check timeout."""

from __future__ import annotations

import copy
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
KEY = "DYN_HEALTH_CHECK_REQUEST_TIMEOUT"


def render(values: Path) -> list[dict]:
    output = subprocess.check_output(
        [
            "helm",
            "template",
            "nemotron35bolt-gb10",
            str(ROOT / "charts/dynamo-model"),
            "--namespace",
            "daedalus-dynamo",
            "--values",
            str(values),
        ],
        text=True,
        timeout=30,
    )
    return [item for item in yaml.safe_load_all(output) if item]


class BoltHealthTimeoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.values = ROOT / "tests/fixtures/retired-values/nemotron35bolt-gb10.yaml"
        cls.rendered = render(cls.values)

    def test_native_worker_timeout_preserves_enabled_probes_and_aliases(self):
        graph = next(
            item for item in self.rendered if item["kind"] == "DynamoGraphDeployment"
        )
        services = graph["spec"]["services"]
        worker = services["VllmWorker"]
        container = worker["extraPodSpec"]["mainContainer"]
        environment = {entry["name"]: entry.get("value") for entry in container["env"]}
        self.assertEqual(environment[KEY], "120")
        self.assertNotEqual(environment.get("DYN_HEALTH_CHECK_ENABLED"), "false")
        self.assertEqual(worker["readinessProbe"]["httpGet"]["path"], "/health")
        self.assertEqual(worker["livenessProbe"]["httpGet"]["path"], "/live")
        self.assertEqual(worker["livenessProbe"]["failureThreshold"], 30)
        self.assertEqual(container["startupProbe"]["httpGet"]["path"], "/live")
        self.assertIn(
            '"Qwen/Qwen3.6-35B-A3B-FP8" "Qwen/Qwen3.8-27B-FP8"', container["args"][0]
        )
        for name, service in services.items():
            if name != "VllmWorker":
                self.assertNotIn(
                    KEY,
                    {
                        entry["name"]
                        for entry in service["extraPodSpec"]["mainContainer"]["env"]
                    },
                )

    def test_entire_render_diff_is_only_one_worker_environment_entry(self):
        baseline = yaml.safe_load(self.values.read_text())
        baseline["worker"]["extraEnv"] = [
            entry for entry in baseline["worker"]["extraEnv"] if entry["name"] != KEY
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "before.yaml"
            path.write_text(yaml.safe_dump(baseline))
            before = render(path)
        without_timeout = copy.deepcopy(self.rendered)
        graph = next(
            item for item in without_timeout if item["kind"] == "DynamoGraphDeployment"
        )
        container = graph["spec"]["services"]["VllmWorker"]["extraPodSpec"][
            "mainContainer"
        ]
        container["env"] = [entry for entry in container["env"] if entry["name"] != KEY]
        self.assertEqual(without_timeout, before)


if __name__ == "__main__":
    unittest.main()
