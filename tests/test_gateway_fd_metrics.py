from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "gateway_fd_metrics", ROOT / "scripts" / "gateway-fd-metrics.py"
)
assert SPEC and SPEC.loader
metrics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(metrics)


class GatewayFDMetricsTests(unittest.TestCase):
    def test_renders_limits_fd_headroom_and_tcp_states(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            process = proc / "42"
            (process / "fd").mkdir(parents=True)
            (process / "net").mkdir()
            (process / "cmdline").write_bytes(b"/app/agentgateway\0-f\0/config/config.yaml\0")
            (process / "limits").write_text(
                "Limit                     Soft Limit           Hard Limit           Units\n"
                "Max open files            131072               524288               files\n"
            )
            os.symlink("socket:[12345]", process / "fd" / "3")
            os.symlink("pipe:[777]", process / "fd" / "4")
            (process / "net" / "tcp").write_text(
                "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode\n"
                "   0: 0100007F:1F90 0100007F:C350 01 00000000:00000000 00:00000000 00000000 10101 0 12345\n"
            )
            (process / "net" / "tcp6").write_text(
                "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode\n"
            )

            rendered, healthy = metrics.render_metrics(proc)

        self.assertTrue(healthy)
        self.assertIn("daedalus_gateway_process_open_fds", rendered)
        self.assertIn(" 2\n", rendered)
        self.assertIn("daedalus_gateway_process_soft_limit_fds", rendered)
        self.assertIn(" 131072\n", rendered)
        self.assertIn('daedalus_gateway_pod_tcp_sockets{', rendered)
        self.assertIn('state="established"} 1', rendered)
        self.assertIn("daedalus_gateway_process_fd_utilization_ratio", rendered)

    def test_reports_down_when_process_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rendered, healthy = metrics.render_metrics(Path(directory))
        self.assertFalse(healthy)
        self.assertIn("daedalus_gateway_fd_exporter_up", rendered)
        self.assertTrue(rendered.endswith(" 0\n"))


if __name__ == "__main__":
    unittest.main()
