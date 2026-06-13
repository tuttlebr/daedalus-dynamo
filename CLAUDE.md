# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Three **self-contained, intentionally parallel** NVIDIA Dynamo deployment
examples, each pairing a model image with the same companion "Photo Tagger" Rust
app. The examples differ only where the model/backend forces them to:

| Folder | Model | Backend | Namespace | Dynamo deployment | UI NodePort | Image repo var |
| --- | --- | --- | --- | --- | --- | --- |
| `nemotron-3-nano-omni-dynamo/` | Nemotron-3-Nano-Omni-30B (FP8) | vLLM | `nemotron-omni` | `nemotron-omni-vllm-agg` | `30150` | `NEMOTRON_IMAGE_REPOSITORY` |
| `qwen-36-35b-dynamo/` | Qwen3.6-35B-A3B (FP8) | vLLM | `qwen36-35b` | `qwen36-dynamo-fd-ec` | `30151` | `QWEN_IMAGE_REPOSITORY` |
| `qwen-36-27b-dynamo/` | Qwen3.6-27B (FP8) | SGLang | `qwen36-27b` | `qwen36-dynamo-fd-ec` | `30152` | `QWEN_IMAGE_REPOSITORY` |

Shared assets live at the repo root: `observability/` (one Grafana dashboard
covering vLLM/SGLang/TRT-LLM) and `scripts/pre-commit/`.

**Core invariant: keep the three examples structurally parallel.** A change to
one example's Photo Tagger, deploy flow, or k8s layout almost always belongs in
all three. The API, UI, DB schema, auth model, and validation flow are meant to
be identical across examples. See `AGENTS.md` for the same guidance in short
form, and `README.md` for the intended user workflow.

## Commands

Run from inside an example folder unless noted.

```bash
make validate                              # shell syntax + Python compile + kustomize render + cargo test (if deps present)
STRICT=1 SERVER_DRY_RUN=1 make validate    # also fail on warnings + server-side kubectl dry-run (needs cluster access)

NAMESPACE=<ns> HF_TOKEN=<token> ./deploy.sh   # build, push, create secrets, apply Daedalus overlay
./deploy.sh render                            # print rendered manifests, no apply
ACTION=delete NAMESPACE=<ns> ./deploy.sh      # tear down (preserves PVCs/secrets unless DELETE_DATA=1)
./test-endpoint.sh                            # smoke-test a port-forwarded Dynamo frontend
docker compose build                          # build both images locally without deploying
```

Rust (Photo Tagger), from repo root or an example folder:

```bash
cargo test  --manifest-path <example>/photo-tagger/Cargo.toml                 # all tests
cargo test  --manifest-path <example>/photo-tagger/Cargo.toml <test_name>     # single test
cargo fmt   --manifest-path <example>/photo-tagger/Cargo.toml -- --check      # what pre-commit enforces
```

`cargo test` needs native HEIF deps or it is **skipped, not failed** (by both
`make validate` and the pre-commit hook). Install on Ubuntu/Debian:
`sudo apt-get install -y build-essential pkg-config libc6-dev libheif-dev zlib1g-dev`.

Pre-commit hooks (`.pre-commit-config.yaml` → `scripts/pre-commit/`) cover repo
hygiene, secret scanning, shell syntax, Python compile, kustomize render, and
Rust fmt/test. `scripts/pre-commit/common.sh` holds the canonical `EXAMPLES`
list — update it when adding/removing an example.

## Per-example layout

```
<example>/
  Dockerfile            # model image: FROM the Dynamo vLLM/SGLang runtime, pip-install extras (no source build)
  docker-compose.yaml   # convenience: build model + photo-tagger images
  deploy.sh             # build/push/secrets/apply; supports deploy|delete|render
  test-endpoint.sh      # frontend smoke test
  Makefile              # `make validate` → scripts/validate.sh
  scripts/validate.sh   # the local check bundle
  k8s/
    base/               # integrated DynamoGraphDeployment + Redis + Postgres + Photo Tagger + NetworkPolicy
    overlays/daedalus/  # NFS mounts, UI NodePort, node affinity, PHOTO_TAGGER_REQUIRE_AUTH=true
    benchmarks/         # opt-in Jobs: aiperf/ and dynosim-aiperf/
  photo-tagger/         # the companion Rust app (see below)
```

## deploy.sh architecture

`deploy.sh` is the single entry point and the most intricate piece of each
example. It:

1. Preflights the cluster (Dynamo + Grove CRDs and operators must be installed).
2. Ensures three secrets exist, **generating and preserving** them if absent —
   never overwriting an existing one: `hf-token-secret` (from `HF_TOKEN`),
   `photo-tagger-postgres` (password), `photo-tagger-auth` (UI token).
3. Renders via a temp kustomization that patches the model image into the
   `DynamoGraphDeployment` and the Photo Tagger image into its `Deployment`,
   using a timestamp `IMAGE_TAG` for repeatable deploys.
4. On `delete`, deletes the `DynamoGraphDeployment` first so the operator cleans
   up Grove children, then sweeps label-selected leftovers. `DELETE_DATA=1`
   removes PVCs/secrets; `FORCE_CLEAR_FINALIZERS=1` unsticks stuck Grove
   finalizers.

Key env overrides (see the header comment in each `deploy.sh`): `REGISTRY`
(default `daedalus.ddns.me:5050/btuttle`), `IMAGE_TAG`, `SKIP_BUILD/PUSH/DEPLOY`,
`BASE_IMAGE`, `DELETE_DATA`, `DELETE_NAMESPACE`.

**GPU-shape gotcha:** when changing an existing deployment to a larger per-worker
GPU count, `delete` before redeploying — a rolling update can deadlock on old
pods holding GPUs.

## Photo Tagger app

A single-binary axum/tokio service (`photo-tagger/src/`) that walks NFS-mounted
photo dirs, sends each unseen image to the example's Dynamo
`/v1/chat/completions` endpoint, and stores structured metadata in Postgres with
full-text search. `main.rs` wires `Config` → `LlmClient` + `Pipeline` +
`JobRegistry` into `AppState`, splits routes into public (`/health`, `/ready`)
vs auth-gated (everything else, via `require_auth` middleware), and runs
background readiness probes for DB and endpoint. Module map: `config` (env),
`walker` (dir scan), `transcode` (image normalize/downscale), `llm` (chat call +
retry + reasoning-strip), `pipeline` (per-file orchestration + process-once),
`db` (Postgres/r2d2), `models` (metadata schema + `SCHEMA_VERSION`/`PROMPT_VERSION`),
`jobs` (in-memory job + SSE), `routes`, `error`.

Behavior worth knowing before editing:

- **Process-once identity = SHA256 of file contents** (rename-tolerant), so
  reruns skip already-processed files; previously-failed files are retried.
- **Model-neutral config**: use `LLM_ENDPOINT` for the chat endpoint.
  `NEMOTRON_ENDPOINT` / `QWEN_ENDPOINT` are accepted only as legacy fallbacks.
- **Roots are validated at startup**: `ALLOWED_ROOTS` (scan boundary) and
  `SIDECAR_ROOTS` (writable `<img>.metadata.json` output) must exist in-pod and
  match the overlay's NFS mounts. Sidecars are skipped on read-only shares;
  Postgres is always authoritative.
- **Backend image-delivery difference** (the one real source-level divergence):
  the vLLM examples inline prepared images as base64 data URLs. The **SGLang
  (27b) example adds `src/images.rs`** — an in-memory `ImageStore` that serves
  short-lived HTTP image URLs (defaults `IMAGE_DELIVERY=url`), because Dynamo's
  SGLang multimodal path rejects `data:` URLs. Don't delete `images.rs` from 27b
  or add it to the vLLM examples when "aligning" the three.

Full endpoint and env reference: `<example>/photo-tagger/README.md`.

## Conventions

- Shell scripts use `set -euo pipefail`. Rust uses `cargo fmt` defaults,
  snake_case, small modules, tests inline under `#[cfg(test)]`.
- Don't commit `target/`, `__pycache__/`, rendered YAML, secrets, or `.env` —
  `.gitignore` and the hygiene/secret hooks guard these.
- Model images are built **on top of** the Dynamo runtime base image (pinned via
  `BASE_IMAGE`); there is no Rust/source build of Dynamo itself.
