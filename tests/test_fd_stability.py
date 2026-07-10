from __future__ import annotations

import importlib.util
import sys
import unittest
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
