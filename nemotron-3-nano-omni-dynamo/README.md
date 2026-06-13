<!--
SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Nemotron 3 Nano Omni NVFP4

Serves [nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-FP8](https://huggingface.co/nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-FP8)
using vLLM with an aggregated Dynamo deployment.

This recipe builds a custom container on top of the Dynamo vLLM runtime image —
no source build, no Rust toolchain.

> Deployment note: use the repository-root `deploy.sh` as the authoritative
> deployment entrypoint. This folder's `deploy.sh` is a compatibility wrapper
> that forwards to `../deploy.sh deploy nemotron`; any older standalone
> Kustomize deployment notes below are backend implementation context.

## Topology

| Role            | Replicas | GPUs/replica | Notes                                                   |
| --------------- | -------- | ------------ | ------------------------------------------------------- |
| Dynamo frontend | 1        | 0            | Prediction-based KV routing, ClusterIP only by default  |
| ModelExpress    | 1        | 0            | Downloads and serves model cache metadata with Redis    |
| vLLM worker     | 2        | 2            | TP=2 workers on separate eligible high-memory GPU nodes |

## Step 1: Deploy Everything

```bash
export NAMESPACE=<your-namespace>
export HF_TOKEN=<your-hf-token>

./deploy.sh
```

`deploy.sh` forwards to the repository-root deployment entrypoint. It builds
and pushes the selected backend image and the single shared Photo Tagger image
unless `DEPLOY_PHOTO_TAGGER=0`, creates required secrets, and applies the shared
Gateway API Inference Extension deployment.

Useful build args:

- `BASE_IMAGE=<image>` — pin to a different Dynamo vLLM runtime base (default `nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.3.0-dev.1-cuda13`).

Useful deploy env overrides:

- `REGISTRY=...` — default `daedalus.ddns.me:5050/btuttle`
- `IMAGE_TAG=...` — default is a UTC timestamp tag for repeatable deploys.
- `ALLOW_LATEST=1` — suppress the warning when explicitly using `IMAGE_TAG=latest`.
- `NEMOTRON_IMAGE_REPOSITORY=...` — default `${REGISTRY}/nemotron-3-nano-omni`.
- `PHOTO_TAGGER_IMAGE_REPOSITORY=...` — default `${REGISTRY}/photo-tagger`.
- `DEPLOY_PHOTO_TAGGER=0` — deploy only the gateway and model backends for a custom OpenAI-compatible app.
- `NAMESPACE=...` — default `daedalus-dynamo`
- `POSTGRES_PASSWORD=...` — optional; used only when creating the Photo Tagger Postgres secret for the first time.
- `PHOTO_TAGGER_AUTH_TOKEN=...` — optional; used only when creating the Photo Tagger auth secret for the first time.
- `PHOTO_TAGGER_AUTH_USER=...` — default `photo-tagger`; HTTP Basic username for the Photo Tagger UI/API.
- `MAX_FILE_BYTES=...` — default `104857600`; rejects oversized images before reading them into memory.
- `MAX_IMAGE_PIXELS=...` — default `80000000`; rejects images above this decoded-pixel count.
- `SKIP_BUILD=1`, `SKIP_PUSH=1`, or `SKIP_DEPLOY=1` for partial runs

### Local image build

`docker-compose.yaml` is a thin convenience for building the backend image without
running the full `deploy.sh` pipeline:

```bash
docker compose build
```

It targets the same `daedalus.ddns.me:5050/btuttle/` image names that
`deploy.sh` uses by default.

## Validation

Run the local validation bundle before applying changes:

```bash
make validate
```

On a Daedalus cluster host, add server-side manifest validation:

```bash
STRICT=1 SERVER_DRY_RUN=1 make validate
```

The validation script checks shell syntax, Python benchmark scripts, Kustomize
rendering, and Rust tests when `cargo` is installed.

For local Photo Tagger Rust tests on Ubuntu/Debian hosts, install the native
build and HEIF development dependencies first:

```bash
sudo apt-get install -y build-essential pkg-config libc6-dev libheif-dev zlib1g-dev
```

The default worker shape starts two TP=2 vLLM workers. On Daedalus this
consumes two L40S GPUs on each of two eligible high-memory GPU nodes.
When changing an existing deployment from a smaller GPU shape, delete the old
deployment before redeploying so the new 2-GPU pods are not blocked by old
1-GPU pods during a rolling update:

```bash
ACTION=delete NAMESPACE=${NAMESPACE} ./deploy.sh
NAMESPACE=${NAMESPACE} HF_TOKEN=${HF_TOKEN} ./deploy.sh
```

## Step 2: Render or Apply

Render this backend through the root deployment path:

```bash
./deploy.sh render
IMAGE_TAG=test ../deploy.sh render nemotron | kubectl apply --dry-run=client -f -
```

Monitor startup:

```bash
kubectl get pods -n ${NAMESPACE} -l nvidia.com/dynamo-graph-deployment-name=nemotron-omni-vllm-agg -w
```

## Step 3: Test

When `DEPLOY_PHOTO_TAGGER=1`, open the shared Photo Tagger UI:

```text
http://<any-cluster-node>:30153/
```

The shared root deployment exposes this UI through NodePort `30153` by default.
It requires HTTP Basic auth by default; use username `photo-tagger` and the
token stored in the `photo-tagger-auth` secret. Keep the NodePort behind trusted
cluster/network boundaries.

```bash
kubectl get secret photo-tagger-auth -n ${NAMESPACE} \
  -o jsonpath='{.data.token}' | base64 -d
```

For direct Nemotron API testing, port-forward the internal Dynamo frontend:

```bash
kubectl port-forward svc/nemotron-omni-vllm-agg-frontend 8000:8000 -n ${NAMESPACE}
```

Then run the bundled smoke test:

```bash
./test-endpoint.sh
```

In another terminal, send a minimal text request:

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-FP8",
    "messages": [{"role": "user", "content": "Hello!"}],
    "max_tokens": 128
  }'
```

To send agentic hints, include top-level `nvext` beside the normal
OpenAI-compatible fields:

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-FP8",
    "messages": [{"role": "user", "content": "Hello!"}],
    "max_tokens": 128,
    "nvext": {
      "agent_hints": {
        "priority": 5,
        "osl": 128,
        "speculative_prefill": true
      },
      "cache_control": {
        "type": "ephemeral",
        "ttl": "1h"
      },
      "extra_fields": ["worker_id", "timing"]
    }
  }'
```

For OpenAI Python clients, pass the same object through `extra_body`:

```python
client.chat.completions.create(
    model="nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-FP8",
    messages=[{"role": "user", "content": "Hello!"}],
    max_tokens=128,
    extra_body={
        "nvext": {
            "agent_hints": {
                "priority": 5,
                "osl": 128,
                "speculative_prefill": True,
            },
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
            "extra_fields": ["worker_id", "timing"],
        }
    },
)
```

The same `nvext` object can be passed by Anthropic-compatible agent harnesses
through `/v1/messages`:

```bash
curl http://localhost:8000/v1/messages \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-FP8",
    "messages": [{"role": "user", "content": "Hello!"}],
    "max_tokens": 128,
    "nvext": {
      "agent_hints": {
        "priority": 5,
        "osl": 128,
        "speculative_prefill": true
      },
      "cache_control": {
        "type": "ephemeral",
        "ttl": "1h"
      },
      "extra_fields": ["worker_id", "timing"]
    }
  }'
```

For streaming tool-call dispatch, stream a tool-capable request and watch for
`tool_call_dispatch` server-sent events before the final response terminates:

```bash
curl -N http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-FP8",
    "stream": true,
    "messages": [{"role": "user", "content": "Call the echo tool with hello."}],
    "tools": [{
      "type": "function",
      "function": {
        "name": "echo",
        "description": "Echo the supplied text.",
        "parameters": {
          "type": "object",
          "properties": {"text": {"type": "string"}},
          "required": ["text"]
        }
      }
    }],
    "tool_choice": {"type": "function", "function": {"name": "echo"}},
    "max_tokens": 128
  }'
```

To exercise the multimodal path, attach an image:

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-FP8",
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

…or an audio clip:

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-FP8",
    "messages": [{
      "role": "user",
      "content": [
        {"type": "audio_url", "audio_url": {"url": "https://raw.githubusercontent.com/yuekaizhang/Triton-ASR-Client/main/datasets/mini_en/wav/1221-135766-0002.wav"}},
        {"type": "text", "text": "Transcribe this audio clip."}
      ]
    }],
    "max_tokens": 256
  }'
```

## Optional: AIPerf Router Benchmark Demo

An opt-in Kubernetes job under `k8s/benchmarks/aiperf` runs AIPerf against the
in-cluster Dynamo frontend using synthetic shared-prefix workloads similar to
the Dynamo router benchmark guide:

```bash
kubectl apply -n ${NAMESPACE} -k k8s/benchmarks/aiperf
kubectl logs -n ${NAMESPACE} job/aiperf-router-demo -f
```

The job sweeps prefix ratios, sends `nvext.agent_hints` with expected output
length and priority, and writes raw artifacts plus `summary.json` /
`summary.csv` to the `aiperf-router-demo-results` PVC.

## Optional: DynoSim + AIPerf Demo

An opt-in Kubernetes job under `k8s/benchmarks/dynosim-aiperf` demonstrates the
DynoSim-to-AIPerf workflow from the upstream
[DynoSim docs](https://github.com/ai-dynamo/dynamo/tree/main/docs/dynosim) and
[NVIDIA blog](https://developer.nvidia.com/blog/dynosim-simulating-the-pareto-frontier).
It runs offline DynoSim with `python -m dynamo.replay` for the same synthetic
shared-prefix workload, then optionally validates that workload against the live
Dynamo frontend with AIPerf:

```bash
kubectl apply -n ${NAMESPACE} -k k8s/benchmarks/dynosim-aiperf
kubectl logs -n ${NAMESPACE} job/dynosim-aiperf-demo -f
```

The job writes raw DynoSim reports, raw AIPerf artifacts, and combined JSON/CSV
summaries to the `dynosim-aiperf-demo-results` PVC. The runtime image must
include a Dynamo build with `python -m dynamo.replay`; set `RUN_AIPERF=false`
in the benchmark kustomization when you want an offline-only simulation smoke
test. It also writes
`kubernetes/dynamo-graph-deployment-request.yaml`, a reviewable
`DynamoGraphDeploymentRequest` with `searchStrategy: rapid` and
`autoApply: false`; applying that manifest lets the Dynamo operator generate
the final `DynamoGraphDeployment` under
`.status.profilingResults.selectedConfig`.

## Key Configuration Notes

- `--enable-multimodal` enables image, video, and audio inputs, and
  `--frontend-decoding` runs media fetch/decode on the frontend.
- `--multimodal-embedding-cache-capacity-gb 4` and `--mm-processor-cache-gb 8`
  cache multimodal embeddings and processor outputs so repeated media reuse warm
  caches instead of recomputing.
- `--media-io-kwargs '{"video": {"num_frames": 256, "fps": 1}}'` samples long
  videos at one frame per second, capped at 256 frames, and
  `--video-pruning-rate 0.5` prunes redundant video tokens.
- `--dyn-tool-call-parser nemotron_nano` and
  `--dyn-reasoning-parser nemotron_nano` enable Nemotron Nano tool-call and
  reasoning parsing.
- The frontend uses `--router-mode kv --no-router-kv-events`, so the KV router
  predicts cache state from routing decisions and waits for one initial worker
  before serving. `--router-track-output-blocks` lets `nvext.agent_hints.osl`
  improve router load accounting, and `DYN_ROUTER_QUEUE_POLICY=wspt` enables
  priority-aware queue ordering under load.
- Worker-side ZMQ KV events are intentionally not enabled for this Nemotron Nano
  model because this vLLM build disables the hybrid KV cache manager when KV
  events are configured, and Nemotron Nano requires hybrid cache handling for
  its mixed attention/Mamba layers.
- The vLLM worker uses `--scheduling-policy priority` so
  `nvext.agent_hints.priority` is honored by the engine, plus explicit prefix
  caching with SHA-256 hashes for deterministic prefix reuse.
- `--enable-anthropic-api`, `--strip-anthropic-preamble`,
  `--enable-streaming-tool-dispatch`, and
  `--enable-streaming-reasoning-dispatch` enable the agent-harness frontend
  path for Anthropic-compatible clients and early tool/reasoning SSE events.
- `VllmWorker` requests two GPUs and keeps required pod anti-affinity in place
  so the two default worker replicas land on different eligible high-memory GPU
  nodes.

## Event plane transport

The Dynamo runtime defaults to NATS for the event plane and connects to a
NATS server if `NATS_SERVER` is set in the environment (the operator
auto-injects this on most clusters). The frontend unsets `NATS_SERVER` and sets
`DYN_EVENT_PLANE=zmq` so the router does not depend on NATS while running in
prediction-based KV mode. It also raises `ulimit -n` from the container default
soft limit before startup so streamed/high-concurrency traffic cannot exhaust
the frontend's file descriptors:

```yaml
mainContainer:
  env:
    - name: DYN_EVENT_PLANE
      value: zmq
    - name: DYN_FRONTEND_NOFILE_LIMIT
      value: "65536"
  command: ["/bin/bash", "-lc"]
  args:
    # Operator-injected NATS_SERVER takes effect even when set to ""; we have
    # to actually unset it before the runtime reads env.
    - >-
      ulimit -n "${DYN_FRONTEND_NOFILE_LIMIT}" &&
      unset NATS_SERVER &&
      exec python3 -m dynamo.frontend ...
```

The worker keeps the operator-injected event-plane defaults because forcing
ZMQ there makes Dynamo auto-generate vLLM KV event config, which is not
compatible with this hybrid Nemotron Nano model. The request plane is set
explicitly with `DYN_REQUEST_PLANE=tcp`.

## Shared app: Photo Tagger

Photo Tagger now lives once at [`../photo-tagger/`](../photo-tagger/). The root
deployment wires that shared reference app to the Gateway API Inference
Extension endpoint:

```text
LLM_ENDPOINT=http://inference-gateway:80/v1/chat/completions
```

Set `DEPLOY_PHOTO_TAGGER=0` when deploying a custom OpenAI-compatible
application instead. Custom apps should use `http://inference-gateway:80/v1` as
their OpenAI base URL and choose a model returned by `/v1/models`.

## File Layout

```text
nemotron-3-nano-omni-dynamo/
  README.md
  Dockerfile                        # Nemotron Omni vLLM image
  docker-compose.yaml               # local image build convenience
  deploy.sh                         # builds, pushes, applies the overlay
  test-endpoint.sh                  # smoke-test the frontend
  Makefile                          # `make validate` entry point
  scripts/
    validate.sh                     # shell/Python/kustomize/cargo checks
  k8s/
    base/                           # backend DynamoGraphDeployment + Redis/PVCs
    overlays/
      daedalus/                     # Daedalus backend defaults
    benchmarks/
      aiperf/                       # opt-in router benchmark Job
      dynosim-aiperf/               # opt-in DynoSim + AIPerf comparison Job
```
