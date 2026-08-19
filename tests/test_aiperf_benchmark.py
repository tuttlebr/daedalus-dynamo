from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_DIR = ROOT / "benchmarks" / "aiperf"
SCRIPT = BENCHMARK_DIR / "run-model-benchmarks.sh"


class AIPerfBenchmarkTests(unittest.TestCase):
    def test_dry_run_covers_every_selected_model_and_redacts_key(self) -> None:
        env = os.environ.copy()
        env.update(
            {
                "MODELS": "example/model-a,example/model-b",
                "OPENAI_API_KEY": "test-key",
                "ARTIFACTS_DIR": "/tmp/aiperf-dry-run",
                "RUN_ID": "unit-test",
                "TOKENIZER": "auto",
            }
        )
        result = subprocess.run(
            [str(SCRIPT), "--dry-run"],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertIn("=== example/model-a ===", result.stdout)
        self.assertIn("=== example/model-b ===", result.stdout)
        self.assertEqual(result.stdout.count("aiperf profile"), 2)
        self.assertIn("--tokenizer example/model-a", result.stdout)
        self.assertIn("--tokenizer example/model-b", result.stdout)
        self.assertIn("X-Dynamo-Session-ID", result.stdout)
        self.assertNotIn("--prompt-input-tokens-block-size", result.stdout)
        self.assertIn("--fixed-schedule", result.stdout)
        self.assertNotIn("--request-count", result.stdout)
        self.assertIn("REDACTED", result.stdout)
        self.assertNotIn("test-key", result.stdout)

    def test_container_pins_the_verified_aiperf_release(self) -> None:
        pyproject = (BENCHMARK_DIR / "pyproject.toml").read_text()
        dockerfile = (BENCHMARK_DIR / "Dockerfile").read_text()
        script = SCRIPT.read_text()

        self.assertIn('"aiperf==0.11.0"', pyproject)
        self.assertIn('org.opencontainers.image.version="0.11.0"', dockerfile)
        self.assertIn('EXPECTED_AIPERF_VERSION="0.11.0"', script)
        self.assertIn('MOONCAKE_BLOCK_SIZE="512"', script)
        self.assertIn(
            'AIPERF_SOURCE_COMMIT="38687855e98044fcf12ee48c6794128f10b6780b"',
            script,
        )
        self.assertIn("output_length_evidence", script)

    def test_agentic_dataset_has_six_growing_turns(self) -> None:
        config = json.loads((BENCHMARK_DIR / "agentic-code-config.json").read_text())

        self.assertEqual(config["turns"]["mean"], 6)
        self.assertEqual(config["turns"]["min"], 6)
        self.assertEqual(config["turns"]["max"], 6)
        self.assertEqual(config["block_size"], 512)
        self.assertEqual(config["cache"]["layer1_tokens"], 1024)
        self.assertEqual(config["cache"]["layer1_5_tokens"], 512)


if __name__ == "__main__":
    unittest.main()
