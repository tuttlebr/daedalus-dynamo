<!--
SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Gemma 4 31B IT

Serves [nvidia/Gemma-4-31B-IT-NVFP4](https://huggingface.co/nvidia/Gemma-4-31B-IT-NVFP4)
using vLLM with the shared repository-root Dynamo Gateway API Inference
Extension deployment.

> Deployment note: use the repository-root `deploy.sh` as the authoritative
> deployment entrypoint. This folder's `deploy.sh` is a compatibility wrapper
> that forwards to `../deploy.sh <action> gemma431b`.

## Topology

| Role | Replicas | GPUs/replica | Notes |
| --- | --- | --- | --- |
| Gateway/EPP | 1 | 0 | Shared Gateway API Inference Extension routing |
| ModelExpress | 1 | 0 | Downloads and serves model cache metadata with Redis |
| vLLM worker | 1 | 1 | TP=1 worker on an eligible high-memory GPU node |

## Runtime Notes

The Hugging Face config identifies the model as `Gemma4ForConditionalGeneration`
with `model_type: gemma4` and a text config max position limit of 262144 tokens.
This deployment uses a single-GPU NVFP4 worker shape so Gemma can be compared
against the other backends without measuring horizontal or tensor-parallel
scale. The DGD intentionally does not set `--max-model-len` or
`--max-num-batched-tokens`; vLLM derives those limits from the model/runtime
configuration.

The root render path enables the Gemma-specific vLLM parser settings:

- `--dyn-tool-call-parser gemma4`
- `--dyn-reasoning-parser gemma4`
- `--limit-mm-per-prompt '{"image":4,"audio":1}'`
- `--tensor-parallel-size 1`
- `--kv-cache-dtype fp8`

Worker-side KV events are disabled for this backend. Gemma 4 mixes sliding and
full attention layers, so this deployment stays conservative until the selected
Dynamo/vLLM runtime is known to support KV event emission for that hybrid cache
layout.

Sources checked while adding this backend:

- vLLM recipe: <https://recipes.vllm.ai/nvidia/Gemma-4-31B-IT-NVFP4>
- Hugging Face config: <https://huggingface.co/nvidia/Gemma-4-31B-IT-NVFP4/blob/main/config.json>

## Deploy

```bash
export NAMESPACE=<your-namespace>
export HF_TOKEN=<your-hf-token>

../deploy.sh deploy gemma431b
```

The model is hosted on Hugging Face, so the deployment needs a token with access
to the repository. `deploy.sh` creates or reuses the `hf-token-secret` secret in
the target namespace.

Useful build and deploy overrides:

- `GEMMA431B_IMAGE_REPOSITORY=...` defaults to `${REGISTRY}/gemma-4-31b-it-dynamo`
- `GEMMA431B_BASE_IMAGE=...` overrides the Dynamo vLLM runtime base image
- Worker replicas, GPU requests, and runtime args are owned by
  `k8s/base/dynamo.yaml`.
- `DEPLOY_WORKBENCH=1` also deploys the shared Routing Workbench

`GEMMA_IMAGE_REPOSITORY` is accepted by this folder's compatibility wrapper as
an alias for `GEMMA431B_IMAGE_REPOSITORY`.

### Local Image Build

```bash
docker compose build
```

## Render Or Apply

Render this backend through the root deployment path:

```bash
./deploy.sh render
IMAGE_TAG=test ../deploy.sh render gemma431b | kubectl apply --dry-run=client -f -
```

Monitor startup:

```bash
kubectl get pods -n ${NAMESPACE} \
  -l nvidia.com/dynamo-graph-deployment-name=gemma-4-31b-it-vllm-agg -w
```

## Test

For direct API testing, port-forward the worker-side Dynamo frontend service
created by the root deployment:

```bash
kubectl port-forward -n ${NAMESPACE} svc/gemma-4-31b-it-vllm-agg-0 8000:8000
```

Then run the bundled smoke test:

```bash
./test-endpoint.sh
```

Minimal chat request:

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "nvidia/Gemma-4-31B-IT-NVFP4",
    "messages": [{"role": "user", "content": "Hello!"}],
    "max_tokens": 128
  }'
```

Image input smoke request:

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "nvidia/Gemma-4-31B-IT-NVFP4",
    "messages": [{
      "role": "user",
      "content": [
        {"type": "image_url", "image_url": {"url": "https://huggingface.co/datasets/huggingface/documentation-images/resolve/main/diffusers/inpaint.png"}},
        {"type": "text", "text": "Describe what is in this image."}
      ]
    }],
    "max_tokens": 256
  }'
```

When `DEPLOY_WORKBENCH=1`, the shared Routing Workbench is exposed through
NodePort `30153` by default:

```text
http://<any-cluster-node>:30153/
```

## Validation

Run the local validation bundle before submitting changes:

```bash
make validate
```

On a Daedalus cluster host, add server-side manifest validation:

```bash
STRICT=1 SERVER_DRY_RUN=1 make validate
```

The validation script checks shell syntax, Python benchmark scripts, Kustomize
rendering, and shared Routing Workbench Rust tests when `cargo` is installed.

## File Layout

```text
gemma-4-31b-it-dynamo/
  README.md
  Dockerfile
  docker-compose.yaml
  deploy.sh
  test-endpoint.sh
  Makefile
  scripts/validate.sh
  k8s/
```
