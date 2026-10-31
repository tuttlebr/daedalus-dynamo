from __future__ import annotations

import os
import socketserver
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER_SCRIPT = ROOT / "charts/dynamo-model-runtime/files/server.sh"


class RedisStub(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        data = self.request.recv(4096)
        if data:
            while not data.endswith(b"mx:model:HuggingFace:test/model\r\n"):
                fragment = self.request.recv(4096)
                if not fragment:
                    break
                data += fragment
            self.server.commands.append(data)
            self.request.sendall(b":1\r\n")


class CacheRuntimeTests(unittest.TestCase):
    def exercise(self, download_fails: bool) -> None:
        with tempfile.TemporaryDirectory() as directory, socketserver.TCPServer(("127.0.0.1", 0), RedisStub) as redis:
            redis.commands = []
            thread = threading.Thread(target=redis.serve_forever, daemon=True)
            thread.start()
            root = Path(directory)
            (root / "modelexpress-server").write_text("#!/bin/bash\nexec sleep 120\n")
            (root / "modelexpress-cli").write_text(
                "#!/bin/bash\n"
                "if [[ $3 == health ]]; then exit 0; fi\n"
                + ("exit 7\n" if download_fails else
                   'mkdir -p "$MODEL_CACHE_PATH/models--test--model/snapshots/$MODEL_REVISION"\n')
            )
            for filename in ("modelexpress-server", "modelexpress-cli"):
                (root / filename).chmod(0o755)
            snapshots = root / "cache/models--test--model/snapshots"
            snapshots.mkdir(parents=True)
            (snapshots / "old-revision").mkdir()
            marker = snapshots / "ready-pinned-revision"
            marker.symlink_to("old-revision")
            environment = {**os.environ, "MODEL_NAME": "test/model", "MODEL_CACHE_PATH": str(root / "cache"),
                           "MODEL_REVISION": "pinned-revision", "MODEL_EXPRESS_URL": "http://model:8000",
                           "MODEL_EXPRESS_SERVER_PORT": "8000", "MX_REDIS_HOST": "127.0.0.1",
                           "MX_REDIS_PORT": str(redis.server_address[1]), "MX_CLIENT_CONFIG_PATH": str(root / "config.yaml")}
            process = subprocess.Popen(["bash", str(SERVER_SCRIPT)], cwd=root, env=environment,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                if download_fails:
                    stdout, stderr = process.communicate(timeout=5)
                    self.assertEqual(process.returncode, 7, stdout + stderr)
                    self.assertFalse(marker.is_symlink(), "failed download must invalidate a stale ready marker")
                else:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline and marker.resolve().name != "pinned-revision":
                        time.sleep(0.02)
                    self.assertEqual(marker.resolve().name, "pinned-revision")
                    self.assertTrue(marker.is_dir())
                    self.assertTrue((snapshots / "old-revision").is_dir())
                self.assertEqual(len(redis.commands), 1)
                self.assertIn(b"mx:model:HuggingFace:test/model", redis.commands[0])
                self.assertNotIn(b"FLUSH", redis.commands[0])
            finally:
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=5)
                redis.shutdown()
                thread.join()

    def test_pinned_download_replaces_only_its_ready_marker(self) -> None:
        self.exercise(False)

    def test_failed_download_never_publishes_stale_snapshot(self) -> None:
        self.exercise(True)


if __name__ == "__main__":
    unittest.main()
