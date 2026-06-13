# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with
code in this repository.

## What this repo is

This repository contains three NVIDIA Dynamo model backends behind one shared
Gateway API Inference Extension deployment and one shared reference application:

| Backend | Folder | Model | Runtime |
| --- | --- | --- | --- |
| `nemotron` | `nemotron-3-nano-omni-dynamo/` | `nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-FP8` | vLLM |
| `qwen35b` | `qwen-36-35b-dynamo/` | `Qwen/Qwen3.6-35B-A3B-FP8` | vLLM |
| `qwen27b` | `qwen-36-27b-dynamo/` | `Qwen/Qwen3.6-27B-FP8` | SGLang |

Shared assets live at the repo root:

- `deploy.sh` renders, applies, deletes, builds, and pushes the shared
  deployment.
- `photo-tagger/` is the single reference Rust app.
- `observability/` contains the shared Grafana dashboard.
- `scripts/pre-commit/` contains repository checks.

Core invariant: keep backend folders structurally parallel, but keep application
code single-sourced under `photo-tagger/`. Do not reintroduce per-backend app
copies or per-backend app Kubernetes manifests.

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
NAMESPACE=<ns> HF_TOKEN=<token> ./deploy.sh deploy nemotron qwen35b qwen27b
./deploy.sh render all
DEPLOY_PHOTO_TAGGER=0 ./deploy.sh deploy all
./deploy.sh delete qwen27b
```

Rust Photo Tagger checks from the repository root:

```bash
cargo test --manifest-path photo-tagger/Cargo.toml
cargo test --manifest-path photo-tagger/Cargo.toml <test_name>
cargo fmt --manifest-path photo-tagger/Cargo.toml -- --check
```

`cargo test` needs native HEIF deps or it is skipped by validation hooks. On
Ubuntu/Debian:

```bash
sudo apt-get install -y build-essential pkg-config libc6-dev libheif-dev zlib1g-dev
```

## Layout

```
deploy.sh                         # single deployment entrypoint
photo-tagger/                     # shared Rust reference app
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

1. Selects one or more backends.
2. Builds and pushes selected backend images.
3. Builds and pushes the single Photo Tagger image when
   `DEPLOY_PHOTO_TAGGER=1`.
4. Ensures shared secrets exist.
5. Renders one Gateway, model catalog, HTTPRoute, and one
   `DynamoGraphDeployment` per selected backend.
6. Renders one shared Photo Tagger deployment when enabled.

The Gateway extracts the OpenAI request body `model` field into
`X-Gateway-Base-Model-Name`, and the HTTPRoute sends `/v1/*` traffic to the
matching backend `InferencePool`. Any OpenAI-compatible app can use:

```text
http://inference-gateway:80/v1
```

Set `DEPLOY_PHOTO_TAGGER=0` when deploying a custom app separately.

## Photo Tagger

Photo Tagger is a single-binary axum/tokio service. It walks mounted photo
directories, sends each unseen image to the configured OpenAI-compatible
`/v1/chat/completions` endpoint, and stores metadata in Postgres with full-text
search.

Module map: `config` (env), `walker` (dir scan), `transcode` (image normalize),
`images` (short-lived image URL store), `llm` (chat call and parser),
`pipeline` (per-file orchestration), `db` (Postgres/r2d2), `models` (metadata
schema), `jobs` (job state and SSE), `routes`, and `error`.

Behavior worth knowing:

- Process-once identity is SHA256 of file contents.
- `LLM_ENDPOINT` and `MODEL_NAME` are required at runtime; root deployment
  injects both.
- `IMAGE_DELIVERY=data-url` inlines images; `IMAGE_DELIVERY=url` serves
  short-lived `/llm-image/{token}` URLs for backends that reject data URLs.
- `ALLOWED_ROOTS` and `SIDECAR_ROOTS` are validated at startup.
- `NEMOTRON_ENDPOINT` and `QWEN_ENDPOINT` are legacy fallbacks only.

Full app reference: `photo-tagger/README.md`.

## Conventions

- Shell scripts use `set -euo pipefail`.
- Rust uses `cargo fmt`, snake_case, small modules, and inline tests under
  `#[cfg(test)]`.
- Do not commit `target/`, `__pycache__/`, rendered YAML, secrets, `.env`,
  model checkpoints, or local kubeconfigs.
- Model images are built on top of the Dynamo runtime base image; there is no
  Rust/source build of Dynamo itself.
