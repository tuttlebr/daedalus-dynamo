# Dynamo Routing Workbench

Dynamo Routing Workbench is the repository's shared text-only demo application.
It calls the Gateway API Inference Extension endpoint through the OpenAI-
compatible API and records routing-relevant request metrics.

The workbench never sends image, audio, video, or multimodal content blocks. It
is intended to exercise GAIE-supported text inference behavior:

- `/v1/models` discovery.
- Model-header routing through the shared gateway.
- EPP worker selection and KV-aware routing effects.
- Worker scaling, shared-prefix reuse, latency, and streaming TTFT.
- Optional Dynamo `nvext.extra_fields` such as `worker_id` and `timing`.

## Runtime API

- `GET /health`
- `GET /ready`
- `GET /api/status`
- `GET /api/models`
- `POST /api/runs`
- `GET /api/runs`
- `GET /api/runs/{id}`
- `GET /api/runs/{id}/events`

Start a run:

```bash
curl -sS http://localhost:3000/api/runs \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "Qwen/Qwen3.6-35B-A3B-FP8",
    "preset": "repeated-shared-prefix",
    "total_requests": 8,
    "concurrency": 2,
    "shared_prefix_tokens": 512,
    "max_tokens": 96,
    "temperature": 0.2,
    "include_nvext": true
  }'
```

Supported presets are `cold-prompt`, `repeated-shared-prefix`,
`mixed-prefixes`, and `priority-hints`.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `LISTEN_ADDR` | `0.0.0.0:3000` | HTTP listen address. |
| `OPENAI_BASE_URL` | `http://inference-gateway:80/v1` | OpenAI-compatible base URL. |
| `WORKBENCH_DEFAULT_MODEL` | required | Default model id sent in run requests. |
| `WORKBENCH_REQUIRE_AUTH` | `false` | Require auth for UI/API routes. |
| `WORKBENCH_AUTH_USER` | `routing-workbench` | HTTP Basic username. |
| `WORKBENCH_AUTH_TOKEN` | unset | HTTP Basic password, bearer token, or `X-Workbench-Token`. |
| `WORKBENCH_REQUEST_TIMEOUT_SECS` | `300` | Per-request timeout. |
| `WORKBENCH_MAX_RUN_REQUESTS` | `128` | Maximum requests accepted for one run. |
| `WORKBENCH_MAX_CONCURRENCY` | `16` | Maximum client-side concurrency accepted for one run. |
| `RUST_LOG` | `info,routing_workbench=info` | Tracing filter. |

## Local Development

```bash
WORKBENCH_DEFAULT_MODEL=Qwen/Qwen3.6-35B-A3B-FP8 \
OPENAI_BASE_URL=http://localhost:8000/v1 \
cargo run --manifest-path routing-workbench/Cargo.toml
```

Run checks:

```bash
cargo fmt --manifest-path routing-workbench/Cargo.toml -- --check
cargo test --manifest-path routing-workbench/Cargo.toml
```
