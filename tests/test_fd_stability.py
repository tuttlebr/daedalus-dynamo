from __future__ import annotations

import importlib.util
import sys
import ssl
import unittest
from unittest.mock import Mock, patch
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "fd_stability", ROOT / "scripts" / "test-endpoint-fd-stability.py"
)
assert SPEC and SPEC.loader
stability = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = stability
SPEC.loader.exec_module(stability)


class FDStabilityTests(unittest.TestCase):
    def test_secure_defaults_and_custom_endpoint_override(self) -> None:
        with patch.dict("os.environ", {}, clear=True), patch.object(sys, "argv", ["client"]):
            args = stability.parse_args()
        self.assertEqual(args.url, "https://daedalus.ddns.me:32359")
        self.assertEqual(args.connect_ip, "192.168.1.12")
        with patch.dict("os.environ", {}, clear=True), patch.object(
            sys, "argv", ["client", "--url", "https://example.org"]
        ):
            self.assertEqual(stability.parse_args().connect_ip, "")

    def test_ip_override_preserves_verified_tls_hostname(self) -> None:
        parsed, _, _ = stability.endpoint_paths("https://daedalus.ddns.me:32359")
        with patch("socket.create_connection", return_value=Mock()) as dial:
            connection = stability.new_connection(parsed, 5, "192.168.1.12")
        self.assertTrue(connection._context.check_hostname)
        self.assertEqual(connection._context.verify_mode, ssl.CERT_REQUIRED)
        with patch.object(connection._context, "wrap_socket", return_value=Mock()) as tls:
            connection.connect()
        self.assertEqual(dial.call_args.args[0], ("192.168.1.12", 32359))
        self.assertEqual(tls.call_args.kwargs["server_hostname"], "daedalus.ddns.me")
        self.assertEqual(connection.host, "daedalus.ddns.me")
        self.assertEqual(
            stability.request_headers("unit-secret")["authorization"],
            "Bearer unit-secret",
        )
        connection.close()

    def test_endpoint_paths_accept_base_and_full_urls(self) -> None:
        _, chat, models = stability.endpoint_paths("http://host:8000")
        self.assertEqual(chat, "/v1/chat/completions")
        self.assertEqual(models, "/v1/models")

        _, chat, models = stability.endpoint_paths(
            "https://host/api/v1/chat/completions"
        )
        self.assertEqual(chat, "/api/v1/chat/completions")
        self.assertEqual(models, "/api/v1/models")

    def test_prometheus_parser_extracts_fd_metrics(self) -> None:
        found = stability.parse_prometheus_metrics(
            '# HELP ignored ignored\n'
            'daedalus_gateway_process_open_fds{pod="gw-1"} 37\n'
            'daedalus_gateway_process_soft_limit_fds{pod="gw-1"} 131072\n'
        )
        self.assertEqual(found["daedalus_gateway_process_open_fds"], 37)
        self.assertEqual(found["daedalus_gateway_process_soft_limit_fds"], 131072)


if __name__ == "__main__":
    unittest.main()
