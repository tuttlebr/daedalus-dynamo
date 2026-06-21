# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with
code in this repository.

## What this repo is

This repository contains three NVIDIA Dynamo model backends behind one shared
Gateway API Inference Extension deployment and one optional shared text-only
demo app:

| Backend | Folder | Model | Runtime |
| --- | --- | --- | --- |
| `nemotron` | `nemotron-3-nano-omni-dynamo/` | `nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-NVFP4` | vLLM |
| `qwen35b` | `qwen-36-35b-dynamo/` | `Qwen/Qwen3.6-35B-A3B-FP8` | vLLM |
| `qwen27b` | `qwen-36-27b-dynamo/` | `Qwen/Qwen3.6-27B-FP8` | SGLang |

Shared assets live at the repo root:

- `deploy.sh` renders, applies, deletes, builds, and pushes the shared
  deployment.
- `routing-workbench/` is the single shared Rust demo app.
- `observability/` contains the shared Grafana dashboard.
- `scripts/pre-commit/` contains repository checks.

Core invariant: keep backend folders structurally parallel, but keep
application code single-sourced under `routing-workbench/`. Do not reintroduce
per-backend app copies or per-backend app Kubernetes manifests.

## Commands

Run backend validation from an example folder:

```bash
make validate
STRICT=1 SERVER_DRY_RUN=1 make validate
docker compose build
./test-endpoint.sh
```

Run shared deployment from the repository root:

```bash
NAMESPACE=<ns> HF_TOKEN=<token> ./deploy.sh deploy
NAMESPACE=<ns> HF_TOKEN=<token> ./deploy.sh deploy all
./deploy.sh render qwen35b
DEPLOY_WORKBENCH=1 ./deploy.sh deploy qwen35b
./deploy.sh delete qwen27b
```

Rust Routing Workbench checks from the repository root:

```bash
cargo test --manifest-path routing-workbench/Cargo.toml
cargo test --manifest-path routing-workbench/Cargo.toml <test_name>
cargo fmt --manifest-path routing-workbench/Cargo.toml -- --check
```

`cargo test` needs native linker prerequisites. On Ubuntu/Debian:

```bash
sudo apt-get install -y build-essential libc6-dev
```

## Layout

```text
deploy.sh                         # single deployment entrypoint
routing-workbench/                # shared Rust text-only demo app
observability/                    # shared dashboard assets
scripts/pre-commit/               # repo checks

<backend>/
  Dockerfile                      # model backend image
  docker-compose.yaml             # local backend image build
  deploy.sh                       # compatibility wrapper to ../deploy.sh
  test-endpoint.sh                # frontend smoke test
  Makefile                        # make validate -> scripts/validate.sh
  scripts/validate.sh             # backend checks plus shared app Rust tests
  k8s/
    base/                         # backend DynamoGraphDeployment, Redis, PVCs
    overlays/daedalus/            # backend-specific overlay
    benchmarks/                   # aiperf and dynosim-aiperf Jobs
```

## Deployment Architecture

Root `deploy.sh`:

1. Defaults to `qwen35b`; `all` is explicit for multi-model demos.
2. Builds and pushes selected backend images.
3. Builds and pushes the optional Routing Workbench image when
   `DEPLOY_WORKBENCH=1`.
4. Ensures shared secrets exist.
5. Renders one Gateway, model catalog, HTTPRoute, and one
   `DynamoGraphDeployment` per selected backend.
6. Renders one shared Routing Workbench deployment when enabled.

The Gateway extracts the OpenAI request body `model` field into
`X-Gateway-Base-Model-Name`, and the HTTPRoute sends `/v1/*` traffic to the
matching backend `InferencePool`. Any OpenAI-compatible app can use:

```text
http://inference-gateway:80/v1
```

Set `DEPLOY_WORKBENCH=1` only when deploying the demo workbench.
`DEPLOY_PHOTO_TAGGER` remains a compatibility alias for the shared demo app
toggle.

## Routing Workbench

Routing Workbench is a single-binary axum/tokio service. It discovers
`/v1/models`, starts text-only benchmark runs, streams OpenAI chat completions
for TTFT measurement, and records per-request latency plus optional Dynamo
`nvext.extra_fields` such as `worker_id` and `timing`.

Module map: `config` (env), `auth` (basic/bearer/header auth), `openai`
(models and streaming chat client), `runs` (in-memory benchmark registry and
request generator), `models` (API DTOs), `routes`, and `error`.

Behavior worth knowing:

- The workbench sends only text `messages[].content` strings.
- Run presets are `cold-prompt`, `repeated-shared-prefix`, `mixed-prefixes`,
  and `priority-hints`.
- `OPENAI_BASE_URL` and `WORKBENCH_DEFAULT_MODEL` are required at runtime; root
  deployment injects both.
- `worker_id` and `timing` are best-effort fields. Runs succeed without them.

Full app reference: `routing-workbench/README.md`.

## Conventions

- Shell scripts use `set -euo pipefail`.
- Rust uses `cargo fmt`, snake_case, small modules, and inline tests under
  `#[cfg(test)]`.
- Do not commit `target/`, `__pycache__/`, rendered YAML, secrets, `.env`,
  model checkpoints, or local kubeconfigs.
- Model images are built on top of the Dynamo runtime base image; there is no
  Rust/source build of Dynamo itself.
