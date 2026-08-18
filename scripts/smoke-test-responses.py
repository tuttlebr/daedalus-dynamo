#!/usr/bin/env python3
"""Send one request to a Dynamo model through the OpenAI Responses API."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import ssl
import sys
from typing import Any
from urllib.parse import SplitResult, urlsplit


DEFAULT_ENDPOINT = "http://192.168.1.234:32359"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=os.environ.get("ENDPOINT_URL", DEFAULT_ENDPOINT),
        help="Endpoint base URL or full /v1/responses URL.",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("MODEL_ID", ""),
        help="Model ID. If omitted, use the first model returned by /v1/models.",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("OPENAI_API_KEY", ""),
        help="Bearer token. Defaults to OPENAI_API_KEY when set.",
    )
    parser.add_argument(
        "--prompt",
        default="Reply with one short sentence confirming that the smoke test passed.",
    )
    parser.add_argument("--max-output-tokens", type=int, default=128)
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args()
    if args.max_output_tokens < 1:
        parser.error("--max-output-tokens must be positive")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    return args


def endpoint_paths(raw_url: str) -> tuple[SplitResult, str, str]:
    parsed = urlsplit(raw_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"invalid endpoint URL: {raw_url}")

    path = parsed.path.rstrip("/")
    if path.endswith("/responses"):
        responses_path = path
        models_path = path[: -len("/responses")] + "/models"
    elif path.endswith("/v1"):
        responses_path = f"{path}/responses"
        models_path = f"{path}/models"
    else:
        prefix = path if path else ""
        responses_path = f"{prefix}/v1/responses"
        models_path = f"{prefix}/v1/models"
    return parsed, responses_path, models_path


def new_connection(
    parsed: SplitResult, timeout: float
) -> http.client.HTTPConnection:
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if parsed.scheme == "https":
        return http.client.HTTPSConnection(
            parsed.hostname,
            port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
    return http.client.HTTPConnection(parsed.hostname, port, timeout=timeout)


def request_headers(api_key: str) -> dict[str, str]:
    headers = {"accept": "application/json", "content-type": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    return headers


def request_json(
    parsed: SplitResult,
    method: str,
    path: str,
    api_key: str,
    timeout: float,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = None
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode()

    connection = new_connection(parsed, timeout)
    try:
        connection.request(method, path, body=body, headers=request_headers(api_key))
        response = connection.getresponse()
        raw_body = response.read()
    finally:
        connection.close()

    decoded = raw_body.decode("utf-8", "replace")
    if not 200 <= response.status < 300:
        raise RuntimeError(
            f"{method} {path} returned HTTP {response.status}: {decoded[:1000]}"
        )
    try:
        data = json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{method} {path} returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{method} {path} returned a non-object JSON response")
    return data


def discover_model(
    parsed: SplitResult, models_path: str, api_key: str, timeout: float
) -> str:
    data = request_json(parsed, "GET", models_path, api_key, timeout)
    models = data.get("data")
    if not isinstance(models, list) or not models:
        raise RuntimeError("model discovery returned no models")
    model_id = models[0].get("id") if isinstance(models[0], dict) else None
    if not isinstance(model_id, str) or not model_id:
        raise RuntimeError("model discovery returned an invalid model ID")
    return model_id


def extract_output_text(response: dict[str, Any]) -> str:
    top_level = response.get("output_text")
    if isinstance(top_level, str) and top_level.strip():
        return top_level.strip()

    text_parts: list[str] = []
    output = response.get("output")
    if not isinstance(output, list):
        return ""
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "output_text":
                continue
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                text_parts.append(text.strip())
    return "\n".join(text_parts)


def validate_response(response: dict[str, Any]) -> str:
    if response.get("error"):
        raise RuntimeError(f"Responses API returned an error: {response['error']}")
    status = response.get("status")
    if status not in (None, "completed"):
        raise RuntimeError(f"Responses API returned status {status!r}")
    output_text = extract_output_text(response)
    if not output_text:
        raise RuntimeError("Responses API returned no assistant output text")
    return output_text


def main() -> int:
    args = parse_args()
    parsed, responses_path, models_path = endpoint_paths(args.url)
    model = args.model or discover_model(
        parsed, models_path, args.api_key, args.timeout
    )
    response = request_json(
        parsed,
        "POST",
        responses_path,
        args.api_key,
        args.timeout,
        {
            "model": model,
            "input": args.prompt,
            "max_output_tokens": args.max_output_tokens,
            "stream": False,
        },
    )
    output_text = validate_response(response)
    print(f"PASS: {responses_path} returned completed output for {model}")
    if response.get("id"):
        print(f"response_id={response['id']}")
    print(f"output={output_text}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, TimeoutError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
