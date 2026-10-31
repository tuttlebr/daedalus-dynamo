from __future__ import annotations

import importlib.util
import sys
import ssl
import unittest
from unittest.mock import Mock, patch
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
    def test_fixed_http_defaults_and_custom_endpoint_override(self) -> None:
        with patch.dict("os.environ", {}, clear=True), patch.object(sys, "argv", ["client"]):
            args = responses_smoke.parse_args()
        self.assertEqual(args.url, "http://daedalus.ddns.me:32360")
        self.assertEqual(args.connect_ip, "192.168.1.12")
        with patch.dict("os.environ", {}, clear=True), patch.object(
            sys, "argv", ["client", "--url", "https://example.org"]
        ):
            self.assertEqual(responses_smoke.parse_args().connect_ip, "")

    def test_ip_override_preserves_verified_tls_hostname(self) -> None:
        parsed, _, _ = responses_smoke.endpoint_paths("https://daedalus.ddns.me:32359")
        with patch("socket.create_connection", return_value=Mock()) as dial:
            connection = responses_smoke.new_connection(parsed, 5, "192.168.1.12")
        self.assertTrue(connection._context.check_hostname)
        self.assertEqual(connection._context.verify_mode, ssl.CERT_REQUIRED)
        with patch.object(connection._context, "wrap_socket", return_value=Mock()) as tls:
            connection.connect()
        self.assertEqual(dial.call_args.args[0], ("192.168.1.12", 32359))
        self.assertEqual(tls.call_args.kwargs["server_hostname"], "daedalus.ddns.me")
        self.assertEqual(connection.host, "daedalus.ddns.me")
        self.assertEqual(
            responses_smoke.request_headers("unit-secret")["authorization"],
            "Bearer unit-secret",
        )
        connection.close()

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
