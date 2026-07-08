# NVIDIA Nemotron 3 Nano 30B A3B NVFP4

Serves [nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4](https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4)
using vLLM with an aggregated Dynamo deployment.

> Deployment note: use the repository-root `deploy.sh` as the authoritative
> deployment entrypoint. This folder's `deploy.sh` is a compatibility wrapper
> that forwards to `../deploy.sh deploy nemotronnano`.

## Topology

| Role | Replicas | GPUs/replica | Notes |
| --- | --- | --- | --- |
| Dynamo frontend | 1 | 0 | Event-backed KV routing, ClusterIP only by default |
| ModelExpress | 1 | 0 | Downloads and serves model cache metadata with Redis |
| vLLM worker | 1 | 1 | TP=1 worker on an eligible high-memory GPU node |

## Deploy

```bash
export NAMESPACE=<your-namespace>
export HF_TOKEN=<your-hf-token>

../deploy.sh doctor
../deploy.sh deploy nemotronnano
```

Useful settings:

- `NEMOTRONNANO_IMAGE_REPOSITORY=...` defaults to `${REGISTRY}/nemotron-3-nano-30b-a3b-nvfp4-dynamo`
- `BASE_IMAGE=<image>` pins a different Dynamo vLLM runtime base

The model requires Hugging Face access through `HF_TOKEN` or an existing
`hf-token-secret`. The vLLM launch keeps `--trust-remote-code`, uses the
Nemotron Nano Dynamo tool/reasoning parser, and intentionally does not set
`--max-model-len` or `--max-num-batched-tokens` so vLLM derives those limits
from the model/runtime configuration. Worker replicas, tensor parallel size,
GPU requests, and runtime args are owned by `k8s/base/dynamo.yaml`.

Sources checked while adding this backend:

- Hugging Face model card and local-app instructions for the NVFP4 model:
  <https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4>
- vLLM Nemotron 3 Nano 30B A3B user guide:
  <https://docs.vllm.ai/projects/recipes/en/latest/NVIDIA/Nemotron-3-Nano-30B-A3B.html>

## Build

```bash
docker compose build
```

## Render

```bash
IMAGE_TAG=test ../deploy.sh render nemotronnano
IMAGE_TAG=test ../deploy.sh render nemotronnano | kubectl apply --dry-run=client -f -
```

## Test

Port-forward the internal Dynamo frontend:

```bash
kubectl port-forward svc/nemotron-3-nano-vllm-agg-frontend 8000:8000 -n ${NAMESPACE}
```

Then run:

```bash
./test-endpoint.sh
```

## Validation

```bash
make validate
```

On a Daedalus cluster host, add server-side manifest validation:

```bash
STRICT=1 SERVER_DRY_RUN=1 make validate
```
