from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "responses_smoke", ROOT / "scripts" / "smoke-test-responses.py"
)
assert SPEC and SPEC.loader
responses_smoke = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = responses_smoke
SPEC.loader.exec_module(responses_smoke)


class ResponsesSmokeTests(unittest.TestCase):
    def test_endpoint_paths_accept_base_and_full_urls(self) -> None:
        _, responses, models = responses_smoke.endpoint_paths("http://host:8000")
        self.assertEqual(responses, "/v1/responses")
        self.assertEqual(models, "/v1/models")

        _, responses, models = responses_smoke.endpoint_paths(
            "https://host/api/v1/responses"
        )
        self.assertEqual(responses, "/api/v1/responses")
        self.assertEqual(models, "/api/v1/models")

    def test_extract_output_text_ignores_reasoning_items(self) -> None:
        response = {
            "output": [
                {"type": "reasoning", "summary": []},
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": "Smoke test passed."}
                    ],
                },
            ]
        }
        self.assertEqual(
            responses_smoke.extract_output_text(response), "Smoke test passed."
        )

    def test_validate_response_rejects_incomplete_output(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            responses_smoke.validate_response(
                {"status": "incomplete", "output": []}
            )


if __name__ == "__main__":
    unittest.main()
