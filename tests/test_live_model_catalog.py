from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("serve_model_catalog", ROOT / "scripts/serve-model-catalog.py")
SERVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVER)


def model(model_id: str = "test/model", **extra: object) -> dict:
    return {"id": model_id, "object": "model", "created": 0, "owned_by": "daedalus", **extra}


class LiveModelCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = SERVER.Catalog({"data": [model()], "backends": {"test/model": ["http://frontend/v1/models"]}})

    def refresh(self, response: list[dict]) -> dict:
        with patch.object(SERVER, "fetch_models", return_value=response):
            self.catalog.refresh()
        return json.loads(self.catalog.snapshot())["data"][0]

    def test_startup_discovers_resolved_auto_limit_and_follows_reconfiguration(self) -> None:
        self.assertEqual(self.refresh([]), model())
        for limit in (131072, 32768):
            actual = self.refresh([model(context_window=limit)])
            self.assertEqual(actual, model(**dict.fromkeys(SERVER.CONTEXT_FIELDS, limit)))

    def test_failed_or_unregistered_backend_drops_stale_limit_but_keeps_id(self) -> None:
        self.refresh([model(context_window=131072)])
        self.assertEqual(self.refresh([]), model())
        self.assertEqual(self.refresh([model("another/model", context_window=262144)]), model())

    def test_context_values_must_be_positive_integers(self) -> None:
        for value in (None, "auto", "131072", 0, -1, True, 8192.5, {}, []):
            with self.subTest(value=value):
                self.assertEqual(self.refresh([model(context_window=value)]), model())
        self.assertEqual(self.refresh([model(max_model_len=8192)])["context_window"], 8192)

    def test_alias_uses_exact_canonical_model_without_changing_public_identity(self) -> None:
        self.catalog = SERVER.Catalog({
            "data": [model("legacy/model", canonical_model="test/model", compatibility_alias=True)],
            "backends": {"legacy/model": ["http://frontend/v1/models"]},
        })
        actual = self.refresh([model("unrelated/model", context_window=4096), model(context_window=32768)])
        self.assertEqual(actual["id"], "legacy/model")
        self.assertTrue(actual["compatibility_alias"])
        self.assertEqual(actual["context_window"], 32768)

    def test_multiple_backends_use_minimum_and_require_all_limits(self) -> None:
        self.catalog.backends = {"test/model": ["http://one", "http://two"]}
        with patch.object(SERVER, "fetch_models", side_effect=lambda url: [model(context_window=8192 if url.endswith("one") else 32768)]):
            self.catalog.refresh()
        self.assertEqual(json.loads(self.catalog.snapshot())["data"][0]["context_window"], 8192)
        with patch.object(SERVER, "fetch_models", side_effect=lambda url: [model(context_window=8192)] if url.endswith("one") else []):
            self.catalog.refresh()
        self.assertEqual(json.loads(self.catalog.snapshot())["data"], [model()])

    def test_configured_or_screening_limits_are_never_used_as_runtime_evidence(self) -> None:
        catalog = SERVER.Catalog({"data": [model(context_window=262144, max_model_len=8192)]})
        self.assertEqual(json.loads(catalog.snapshot()), {"object": "list", "data": [model()]})

    def test_bad_frontend_responses_are_isolated(self) -> None:
        for body in (b"not json", b"[]", b'{"data":null}', b'{"data":{}}'):
            with self.subTest(body=body), patch.object(SERVER, "urlopen") as request:
                request.return_value.__enter__.return_value.read.return_value = body
                with self.assertLogs(level="WARNING"):
                    self.assertEqual(SERVER.fetch_models("http://bad"), [])
        for error in (TimeoutError(), SERVER.HTTPException("truncated")):
            with patch.object(SERVER, "urlopen", side_effect=error), self.assertLogs(level="WARNING"):
                self.assertEqual(SERVER.fetch_models("http://bad"), [])

    def test_http_refresh_publishes_limits_without_exposing_backend_addresses(self) -> None:
        class Upstream(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                body = json.dumps({"data": [model(context_window=65536)]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        backend = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        public = ThreadingHTTPServer(("127.0.0.1", 0), SERVER.handler_for(self.catalog))
        self.catalog.backends = {"test/model": [f"http://127.0.0.1:{backend.server_port}/v1/models"]}
        threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (backend, public)]
        for thread in threads:
            thread.start()
        try:
            self.catalog.refresh()
            with urlopen(f"http://127.0.0.1:{public.server_port}/v1/models?test=1", timeout=3) as response:
                body = response.read()
                self.assertEqual(response.status, 200)
            self.assertNotIn(b"127.0.0.1", body)
            self.assertEqual(json.loads(body)["data"][0]["context_window"], 65536)
        finally:
            for server in (backend, public):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=3)


class CatalogDeploymentTests(unittest.TestCase):
    def test_routes_supply_actual_service_backends_and_merge_duplicate_ids(self) -> None:
        def route(service: str, **backend: object) -> dict:
            return {
                "metadata": {"namespace": "models", "annotations": {"daedalus-dynamo/model-id": "test/model"}},
                "spec": {"rules": [{"backendRefs": [{"name": service, "port": 8000, **backend}]}]},
            }
        routes = {"items": [route("first"), route("second", namespace="other"), route("ignored", weight=0)]}
        result = subprocess.run(
            [sys.executable, "scripts/http-routes-to-models-json.py", "--include-backends"],
            cwd=ROOT, input=json.dumps(routes), text=True, capture_output=True, check=True,
        )
        config = json.loads(result.stdout)
        self.assertEqual(config["data"], [model()])
        self.assertEqual(config["backends"], {"test/model": [
            "http://first.models.svc:8000/v1/models", "http://second.other.svc:8000/v1/models",
        ]})

    def test_rendered_catalog_runs_checked_in_server_and_hashes_code_and_configuration(self) -> None:
        config = {"data": [model()], "backends": {"test/model": ["http://frontend:8000/v1/models"]}}
        result = subprocess.run(
            ["scripts/render-inference-gateway.sh", "catalog"], cwd=ROOT,
            env={**os.environ, "MODELS_JSON": json.dumps(config)}, text=True, capture_output=True, check=True,
        )
        documents = {item["kind"]: item for item in yaml.safe_load_all(result.stdout)}
        cm = documents["ConfigMap"]
        self.assertEqual(cm["data"]["serve-model-catalog.py"], (ROOT / "scripts/serve-model-catalog.py").read_text())
        self.assertEqual(json.loads(cm["data"]["models.json"]), config)
        template = documents["Deployment"]["spec"]["template"]
        self.assertIn("daedalus-dynamo/catalog-script-sha256", template["metadata"]["annotations"])
        self.assertIn("daedalus-dynamo/models-json-sha256", template["metadata"]["annotations"])
        container = template["spec"]["containers"][0]
        self.assertEqual(container["command"], ["python", "/opt/catalog/serve-model-catalog.py"])
        self.assertEqual(container["readinessProbe"]["httpGet"]["path"], "/v1/models")
        self.assertEqual(template["spec"]["volumes"][0]["configMap"]["name"], cm["metadata"]["name"])


if __name__ == "__main__":
    unittest.main()
