#!/usr/bin/env python3
"""Publish routed model IDs with context limits discovered from live frontends."""

from __future__ import annotations

import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import urlopen

CONTEXT_FIELDS = ("context_window", "max_model_len", "context_length")
LOGGER = logging.getLogger(__name__)


def fetch_models(url: str) -> list[dict]:
    """A failed frontend must not take down the shared model list."""
    try:
        with urlopen(url, timeout=3) as response:
            payload = json.load(response)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise TypeError("Expected a model list")
        return [model for model in payload["data"] if isinstance(model, dict)]
    except (OSError, ValueError, TypeError, HTTPException) as error:
        LOGGER.warning("Cannot refresh model metadata from %s: %s", url, error)
        return []


def context_limit(models: list[dict], model_id: str) -> int | None:
    """Only accept positive token counts for the exact requested model."""
    matches = [model for model in models if model.get("id") == model_id]
    limits = []
    for model in matches:
        values = [model.get(field) for field in CONTEXT_FIELDS]
        valid = [value for value in values if type(value) is int and value > 0]
        if not valid:
            return None
        limits.append(min(valid))
    return min(limits) if limits else None


class Catalog:
    def __init__(self, config: dict) -> None:
        self.models = config.get("data", [])
        self.backends = config.get("backends", {})
        self.lock = threading.Lock()
        self.body = self.encode({})

    def encode(self, upstream: dict[str, list[dict]]) -> bytes:
        models = []
        for entry in self.models:
            model = {key: value for key, value in entry.items() if key not in CONTEXT_FIELDS}
            # Reviewed aliases use the canonical model's limits but retain their ID.
            model_id = model.get("canonical_model", model["id"])
            limits = [context_limit(upstream.get(url, []), model_id)
                      for url in self.backends.get(model["id"], [])]
            # All routed backends must report a limit; use the smallest of them.
            if limits and all(limit is not None for limit in limits):
                model.update(dict.fromkeys(CONTEXT_FIELDS, min(limits)))
            models.append(model)
        return json.dumps({"object": "list", "data": models}, separators=(",", ":")).encode()

    def refresh(self) -> None:
        urls = sorted({url for urls in self.backends.values() for url in urls})
        with ThreadPoolExecutor(max_workers=8) as pool:
            upstream = dict(zip(urls, pool.map(fetch_models, urls)))
        body = self.encode(upstream)
        with self.lock:
            self.body = body

    def snapshot(self) -> bytes:
        with self.lock:
            return self.body

    def poll(self, stop: threading.Event, interval: float = 30) -> None:
        while not stop.is_set():
            self.refresh()
            if stop.wait(interval):
                break


def handler_for(catalog: Catalog) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            return

        def do_GET(self) -> None:
            if self.path.split("?", 1)[0] != "/v1/models":
                self.send_response(404)
                self.end_headers()
                return
            body = catalog.snapshot()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self.send_header("access-control-allow-origin", "*")
            self.send_header("access-control-allow-methods", "GET,OPTIONS")
            self.send_header("access-control-allow-headers", "content-type,authorization")
            self.end_headers()

    return Handler


def main() -> None:
    catalog = Catalog(json.loads(os.environ.get("MODELS_JSON", '{"object":"list","data":[]}')))
    stop = threading.Event()
    threading.Thread(target=catalog.poll, args=(stop,), daemon=True).start()
    try:
        ThreadingHTTPServer(("0.0.0.0", 8000), handler_for(catalog)).serve_forever()
    finally:
        stop.set()


if __name__ == "__main__":
    main()
