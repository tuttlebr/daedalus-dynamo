# Repository Guidelines

## Project Structure & Module Organization

This repository contains three NVIDIA Dynamo backend examples behind one shared
Gateway API Inference Extension deployment:

- `nemotron-3-nano-omni-dynamo/` for Nemotron Omni on vLLM.
- `qwen-36-35b-dynamo/` for Qwen 35B on vLLM.
- `qwen-36-27b-dynamo/` for Qwen 27B on SGLang.

Each example keeps its top-level `README.md`, `Dockerfile`,
`docker-compose.yaml`, `test-endpoint.sh`, `Makefile`, `scripts/`, and `k8s/`.
Example-level `deploy.sh` files are compatibility wrappers only; the
repository-root `deploy.sh` is the single source of deployment configuration.
Shared Grafana dashboard assets live under `observability/`. The shared Rust
reference app lives once in the repository-level `photo-tagger/` directory, UI
assets in `photo-tagger/static/`, and Rust tests are inline under `#[cfg(test)]`.

## Build, Test, and Development Commands

Run validation/build commands from an example folder unless noted:

- `make validate` checks shell syntax, Python benchmark scripts, Kustomize
  rendering, and Photo Tagger Rust tests when local dependencies are available.
- `STRICT=1 SERVER_DRY_RUN=1 make validate` adds stricter failures and
  server-side Kubernetes validation on a configured cluster host.
- `docker compose build` inside an example builds that backend image.
- From the repository root, `docker build -t <image> -f photo-tagger/Dockerfile
  photo-tagger` builds the shared reference app image.
- From the repository root, `NAMESPACE=<ns> HF_TOKEN=<token> ./deploy.sh deploy
  <backend...>` builds, pushes, creates secrets, and applies the shared GAIE
  deployment.
- From the repository root, `./deploy.sh render nemotron qwen35b qwen27b`
  prints rendered Kubernetes manifests.
- `./test-endpoint.sh` smoke-tests the port-forwarded Dynamo frontend.

## Coding Style & Naming Conventions

Keep the three backend examples structurally parallel. Keep application code in
the single `photo-tagger/` tree, and prefer model-neutral app settings such as
`LLM_ENDPOINT`; keep model-specific names only where required, for example
`NEMOTRON_IMAGE_REPOSITORY` or `QWEN_IMAGE_REPOSITORY`. Rust code uses standard
`cargo fmt` style, snake_case modules/functions, and small focused modules.
Shell scripts should use `set -euo pipefail`.

## Testing Guidelines

Use `make validate` before submitting changes in any modified example. For Rust
changes, add focused unit tests beside the code under `#[cfg(test)]`. For
Kubernetes changes, confirm the root render path succeeds, for example
`IMAGE_TAG=test ./deploy.sh render nemotron qwen35b qwen27b | kubectl apply
--dry-run=client -f -`. Avoid committing generated artifacts such as `target/`,
`__pycache__/`, or rendered YAML.

See `PRE_COMMIT.md` for recommended hook coverage and checks to keep out of
pre-commit.

## Commit & Pull Request Guidelines

This workspace does not include Git history, so use concise imperative commit
messages such as `Align photo-tagger endpoint config`. Pull requests should
state which example folders changed, whether behavior is shared or
backend-specific, and include validation output. For UI-facing changes, include
a screenshot or clear browser verification notes.

## Security & Configuration Tips

Do not commit tokens, rendered secrets, or local `.env` files. Keep NodePorts
behind trusted networks, and document any new secret, PVC, NFS mount, or
cluster prerequisite in the affected example README.
