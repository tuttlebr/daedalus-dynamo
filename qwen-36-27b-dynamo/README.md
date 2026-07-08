<!--
SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Qwen3.6 27B NVFP4

Serves [nvidia/Qwen3.6-27B-NVFP4](https://huggingface.co/nvidia/Qwen3.6-27B-NVFP4), a
dense 27B vision-language model, using vLLM with an aggregated Dynamo
deployment.

This recipe builds a custom container on top of the Dynamo vLLM runtime image —
no source build, no Rust toolchain.

> Deployment note: use the repository-root `deploy.sh` as the authoritative
> deployment entrypoint. This folder's `deploy.sh` is a compatibility wrapper
> that forwards to `../deploy.sh deploy qwen27b`; any older standalone
> Kustomize deployment notes below are backend implementation context.

## Topology

| Role            | Replicas | GPUs/replica | Notes                                                   |
| --------------- | -------- | ------------ | ------------------------------------------------------- |
| Dynamo frontend | 1        | 0            | Event-backed KV routing, ClusterIP only by default      |
| ModelExpress    | 1        | 0            | Downloads and serves model cache metadata with Redis    |
| vLLM worker     | 1        | 1            | TP=1 worker on an eligible high-memory GPU node         |

## Step 1: Deploy Everything

> Cluster check first: run `../deploy.sh doctor` (or `./deploy.sh doctor`)
> before deploying to verify cluster readiness, including RDMA/InfiniBand.

```bash
export NAMESPACE=<your-namespace>
export HF_TOKEN=<your-hf-token>

./deploy.sh
```

`deploy.sh` forwards to the repository-root deployment entrypoint. It builds
and pushes the selected backend image, creates required secrets, and applies the
shared Gateway API Inference Extension deployment. Set `DEPLOY_WORKBENCH=1` to
also build, push, and deploy the optional shared Routing Workbench image.

Useful build args:

- `BASE_IMAGE=<image>` — pin to a different Dynamo vLLM runtime base (default `nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.3.0-dev.1-cuda13`).

Useful deploy env overrides:

- `REGISTRY=...` — default `daedalus.ddns.me:5050/btuttle`
- `IMAGE_TAG=...` — default is a UTC timestamp tag for repeatable deploys.
- `ALLOW_LATEST=1` — suppress the warning when explicitly using `IMAGE_TAG=latest`.
- `QWEN_IMAGE_REPOSITORY=...` — default `${REGISTRY}/qwen36-27b-dynamo`.
- `WORKBENCH_IMAGE_REPOSITORY=...` — default `${REGISTRY}/routing-workbench`.
- `DEPLOY_WORKBENCH=1` — deploy the optional shared Routing Workbench.
- `DEPLOY_PHOTO_TAGGER=...` — compatibility alias for `DEPLOY_WORKBENCH`.
- `NAMESPACE=...` — default `daedalus-dynamo`
- `WORKBENCH_AUTH_TOKEN=...` — optional; used only when creating the Workbench auth secret for the first time.
- `WORKBENCH_AUTH_USER=...` — default `routing-workbench`; HTTP Basic username for the Workbench UI/API.
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

For local Routing Workbench Rust tests on Ubuntu/Debian hosts, install native
linker prerequisites first:

```bash
sudo apt-get install -y build-essential libc6-dev
```

The default worker shape starts one TP=1 vLLM worker. On Daedalus this consumes
one L40S GPU on one eligible high-memory GPU node.
When changing an existing deployment from the older SGLang shape, delete the old
deployment before redeploying so the operator recreates the worker pods with the
new runtime and resource shape:

```bash
NAMESPACE=${NAMESPACE} ./deploy.sh delete
NAMESPACE=${NAMESPACE} HF_TOKEN=${HF_TOKEN} ./deploy.sh
```

## Step 2: Render or Apply

Render this backend through the root deployment path:

```bash
./deploy.sh render
IMAGE_TAG=test ../deploy.sh render qwen27b | kubectl apply --dry-run=client -f -
```

Monitor startup:

```bash
kubectl get pods -n ${NAMESPACE} -l nvidia.com/dynamo-graph-deployment-name=qwen36-27b-vllm-agg -w
```

## Step 3: Test

When `DEPLOY_WORKBENCH=1`, open the shared Routing Workbench UI:

```text
http://<any-cluster-node>:30153/
```

The shared root deployment exposes this UI through NodePort `30153` by default.
It requires HTTP Basic auth by default; use username `routing-workbench` and the
token stored in the `routing-workbench-auth` secret. Keep the NodePort behind
trusted cluster/network boundaries.

```bash
kubectl get secret routing-workbench-auth -n ${NAMESPACE} \
  -o jsonpath='{.data.token}' | base64 -d
```

For direct Qwen API testing, port-forward the internal Dynamo frontend:

```bash
kubectl port-forward svc/qwen36-27b-vllm-agg-frontend 8000:8000 -n ${NAMESPACE}
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
    "model": "nvidia/Qwen3.6-27B-NVFP4",
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
    "model": "nvidia/Qwen3.6-27B-NVFP4",
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
    model="nvidia/Qwen3.6-27B-NVFP4",
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
    "model": "nvidia/Qwen3.6-27B-NVFP4",
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
    "model": "nvidia/Qwen3.6-27B-NVFP4",
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
    "model": "nvidia/Qwen3.6-27B-NVFP4",
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

- The worker runs `python3 -m dynamo.vllm` with `--enable-multimodal` and
  `--frontend-decoding` for Qwen image inputs.
- `--multimodal-embedding-cache-capacity-gb 4` and
  `--mm-processor-cache-gb 8` match the Qwen vLLM embedding-cache shape used by
  the 35B backend.
- `--dyn-tool-call-parser qwen3_coder` and `--dyn-reasoning-parser qwen3`
  enable Qwen tool-call and reasoning parsing.
- The DGD intentionally does not set `--max-model-len`,
  `--max-num-batched-tokens`, or request-level `max-tokens`; vLLM derives those
  limits from the model/runtime configuration.
- The frontend uses `--router-mode kv --no-router-kv-events`, so the KV router
  predicts cache state from routing decisions and waits for one initial worker
  before serving.
- The vLLM worker uses `--scheduling-policy priority` so
  `nvext.agent_hints.priority` is honored by the engine, plus explicit prefix
  caching with SHA-256 hashes for deterministic prefix reuse.
- `--enable-anthropic-api`, `--strip-anthropic-preamble`,
  `--enable-streaming-tool-dispatch`, and
  `--enable-streaming-reasoning-dispatch` enable the agent-harness frontend
  path for Anthropic-compatible clients and early tool/reasoning SSE events.
- `VllmWorker` requests one GPU and keeps required node affinity for eligible
  high-memory GPU nodes.

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

The worker also sets `DYN_EVENT_PLANE=zmq`. Because the frontend runs the KV
router in prediction-based mode (`--no-router-kv-events`), the worker does not
need to publish worker-side KV events. The request plane is set explicitly with
`DYN_REQUEST_PLANE=tcp`.

## Shared app: Routing Workbench

Routing Workbench lives once at
[`../routing-workbench/`](../routing-workbench/). The root deployment wires that
shared text-only demo app to the Gateway API Inference Extension endpoint:

```text
OPENAI_BASE_URL=http://inference-gateway:32359/v1
```

Set `DEPLOY_WORKBENCH=1` when you want this demo app. Custom apps should use
`http://inference-gateway:32359/v1` as their OpenAI base URL and choose a model
returned by `/v1/models`.

## File Layout

```text
qwen-36-27b-dynamo/
  README.md
  Dockerfile                        # Qwen vLLM image
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
