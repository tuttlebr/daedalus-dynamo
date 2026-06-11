<!--
SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Qwen3.6 27B FP8

Serves [Qwen/Qwen3.6-27B-FP8](https://huggingface.co/Qwen/Qwen3.6-27B-FP8), a
dense 27B vision-language model, using SGLang with an aggregated Dynamo
deployment.

This recipe builds a custom container on top of the Dynamo SGLang runtime image —
no source build, no Rust toolchain.

## Topology

| Role            | Replicas | GPUs/replica | Notes                                                   |
| --------------- | -------- | ------------ | ------------------------------------------------------- |
| Dynamo frontend | 1        | 0            | Event-backed KV routing, ClusterIP only by default      |
| ModelExpress    | 1        | 0            | Downloads and serves model cache metadata with Redis    |
| SGLang worker   | 2        | 2            | TP=2 workers on separate eligible high-memory GPU nodes |
| Photo Tagger    | 1        | 0            | Rust UI/API that scans NFS-mounted photo folders        |
| Photo Tagger DB | 1        | 0            | Postgres StatefulSet for metadata and full-text search  |

## Step 1: Deploy Everything

```bash
export NAMESPACE=<your-namespace>
export HF_TOKEN=<your-hf-token>

./deploy.sh
```

`deploy.sh` builds and pushes both images, creates/updates the namespace and
`hf-token-secret`, creates the Photo Tagger Postgres password secret and auth
token secret if they are missing, then applies the integrated Kustomize overlay.

Useful build args:

- `BASE_IMAGE=<image>` — pin to a different Dynamo SGLang runtime base (default `nvcr.io/nvidia/ai-dynamo/sglang-runtime:1.3.0-dev.1-cuda13`).

Useful deploy env overrides:

- `REGISTRY=...` — default `daedalus.ddns.me:5050/btuttle`
- `IMAGE_TAG=...` — default is a UTC timestamp tag for repeatable deploys.
- `ALLOW_LATEST=1` — suppress the warning when explicitly using `IMAGE_TAG=latest`.
- `QWEN_IMAGE_REPOSITORY=...` — default `${REGISTRY}/qwen36-27b-dynamo`.
- `PHOTO_TAGGER_IMAGE_REPOSITORY=...` — default `${REGISTRY}/photo-tagger`.
- `NAMESPACE=...` — default `qwen36-27b`
- `POSTGRES_PASSWORD=...` — optional; used only when creating the Photo Tagger Postgres secret for the first time.
- `PHOTO_TAGGER_AUTH_TOKEN=...` — optional; used only when creating the Photo Tagger auth secret for the first time.
- `PHOTO_TAGGER_AUTH_USER=...` — default `photo-tagger`; HTTP Basic username for the Photo Tagger UI/API.
- `MAX_FILE_BYTES=...` — default `104857600`; rejects oversized images before reading them into memory.
- `MAX_IMAGE_PIXELS=...` — default `80000000`; rejects images above this decoded-pixel count.
- `SKIP_BUILD=1`, `SKIP_PUSH=1`, or `SKIP_DEPLOY=1` for partial runs

### Local image build

`docker-compose.yaml` is a thin convenience for building both images without
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

The default worker shape starts two TP=2 SGLang workers. On Daedalus this
consumes two L40S GPUs on each of two eligible high-memory GPU nodes.
When changing an existing deployment from a smaller GPU shape, delete the old
deployment before redeploying so the new 2-GPU pods are not blocked by old
1-GPU pods during a rolling update:

```bash
ACTION=delete NAMESPACE=${NAMESPACE} ./deploy.sh
NAMESPACE=${NAMESPACE} HF_TOKEN=${HF_TOKEN} ./deploy.sh
```

## Step 2: Manual Apply

If images, `hf-token-secret`, `photo-tagger-postgres`, and
`photo-tagger-auth` already exist, the Daedalus overlay can still be applied
directly:

```bash
kubectl create secret generic photo-tagger-postgres \
  --from-literal=password='<strong-password>' \
  -n ${NAMESPACE} \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl create secret generic photo-tagger-auth \
  --from-literal=token='<strong-ui-token>' \
  -n ${NAMESPACE} \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -n ${NAMESPACE} -k k8s/overlays/daedalus
```

Monitor startup:

```bash
kubectl get pods -n ${NAMESPACE} -l nvidia.com/dynamo-graph-deployment-name=qwen36-dynamo-fd-ec -w
```

## Step 3: Test

Open the Photo Tagger UI:

```text
http://<any-cluster-node>:30152/
```

The Daedalus overlay intentionally exposes this UI through NodePort `30152` for
the demo-prod workflow. It requires HTTP Basic auth by default; use username
`photo-tagger` and the token stored in the `photo-tagger-auth` secret. Keep the
NodePort behind trusted cluster/network boundaries.

```bash
kubectl get secret photo-tagger-auth -n ${NAMESPACE} \
  -o jsonpath='{.data.token}' | base64 -d
```

Only `/health` and `/ready` are unauthenticated. The UI and API also accept the
same token as `Authorization: Bearer <token>` or `X-Photo-Tagger-Token`.

For direct Qwen API testing, port-forward the internal Dynamo frontend:

```bash
kubectl port-forward svc/qwen36-dynamo-fd-ec-frontend 8000:8000 -n ${NAMESPACE}
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
    "model": "Qwen/Qwen3.6-27B-FP8",
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
    "model": "Qwen/Qwen3.6-27B-FP8",
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
    model="Qwen/Qwen3.6-27B-FP8",
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
    "model": "Qwen/Qwen3.6-27B-FP8",
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
    "model": "Qwen/Qwen3.6-27B-FP8",
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
    "model": "Qwen/Qwen3.6-27B-FP8",
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

- The worker runs `python3 -m dynamo.sglang`, which passes native SGLang engine
  arguments straight through. `Qwen/Qwen3.6-27B-FP8` is a vision-language model,
  so SGLang auto-detects and serves its image path with no special flag — there
  is no `--enable-multimodal`/embedding-cache equivalent on this backend.
- Images must be supplied as HTTP(S) URLs; the Dynamo SGLang multimodal path
  does not accept base64 `data:` URLs. The Photo Tagger therefore serves each
  prepared image over HTTP and hands the worker a URL (`IMAGE_DELIVERY=url`) —
  see [`photo-tagger/README.md`](./photo-tagger/README.md). The `image_url`
  examples below already use HTTP URLs and work as-is.
- `--dyn-tool-call-parser qwen3_coder` and `--dyn-reasoning-parser qwen3`
  enable Qwen tool-call and reasoning parsing. Dynamo's frontend does the
  parsing, and these `--dyn-*` flags override SGLang's native parsers.
- `--context-length 131072` caps the served context at 128K (the model is
  natively ~256K-capable); `--mem-fraction-static 0.85` sizes the static
  weight + KV-pool allocation and is the main OOM/throughput tuning knob at TP=2.
- `--chunked-prefill-size 8192` and `--max-running-requests 8` bound the prefill
  chunk size and the per-worker concurrent-request ceiling. RadixAttention
  prefix caching is on by default (disable with `--disable-radix-cache`).
- The frontend uses `--router-mode kv --no-router-kv-events`, so the KV router
  predicts cache state from routing decisions and waits for one initial worker
  before serving.
- `--schedule-policy priority --enable-priority-scheduling` make the SGLang
  scheduler honor `nvext.agent_hints.priority` (higher value scheduled first).
- `--enable-metrics` exposes `sglang:*` Prometheus metrics on the worker's
  Dynamo system port (scraped on :9090) for the Grafana dashboard.
- `--enable-anthropic-api`, `--strip-anthropic-preamble`,
  `--enable-streaming-tool-dispatch`, and
  `--enable-streaming-reasoning-dispatch` enable the agent-harness frontend
  path for Anthropic-compatible clients and early tool/reasoning SSE events.
- `SGLangWorker` requests two GPUs and keeps required pod anti-affinity in place
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

The worker also sets `DYN_EVENT_PLANE=zmq`. Because the frontend runs the KV
router in prediction-based mode (`--no-router-kv-events`), the worker does not
need to publish worker-side KV events. The request plane is set explicitly with
`DYN_REQUEST_PLANE=tcp`.

## Companion app: Photo Tagger

[`photo-tagger/`](./photo-tagger/) is a Rust service that walks NAS-resident
photo directories, sends each unseen image to this Qwen endpoint, and
persists structured metadata for text-based search. It ships with a
single-page UI for triggering scans, watching SSE progress, browsing recently
processed images, and full-text search.

Highlights:

- **Process-once guarantee**: identity = SHA256 of file contents (rename-tolerant);
  state survives pod restarts in the `photo-tagger-postgres` StatefulSet. Files
  that previously failed are retried on later scans instead of being skipped.
- **Protected UI/API**: the Daedalus overlay requires auth for all UI/API routes
  except `/health` and `/ready`; `deploy.sh` creates `photo-tagger-auth` when it
  is missing.
- **Format normalization**: PNG/JPG sent verbatim; WebP / GIF / BMP / TIFF
  transcoded to JPEG q85; oversized images downscaled to 2048px on the longest
  edge before submission. `MAX_FILE_BYTES` and `MAX_IMAGE_PIXELS` reject
  unexpectedly large inputs before expensive processing.
- **Observable scan outcomes**: jobs report `completed`, `completed_with_failures`,
  or `failed`, and directory-walk errors are counted in job progress.
- **Defensive JSON**: requests `response_format: json_object` and strips any
  leaked `<think>...</think>` reasoning blocks before parsing. Parsed metadata is
  validated for expected setting, age-range, quality, and score values.
- **Search-friendly schema**: each row stores caption, multi-sentence
  description, 5–25 tags, scene, setting type, OCR-extracted text, dominant
  colors, people observations, objects, and a quality assessment. Indexed via
  Postgres full-text search.
- **Sidecar export**: writes `<image>.metadata.json` next to each image so the
  metadata is browsable on the NAS without going through the service.
- **Scoped database access**: the base manifests include a NetworkPolicy allowing
  Postgres ingress only from the Photo Tagger pod.

Deploy:

```bash
HF_TOKEN=<your-hf-token> NAMESPACE=${NAMESPACE:-default} ./deploy.sh
# UI on NodePort 30152 — http://<any-cluster-node>:30152/
```

NAS shares are mounted directly via NFS volumes inside the pod (no `hostPath`
dependency on the worker node) — the deployment defaults to mounting
`192.168.1.232:/volume1/homes` read-only and `192.168.1.77:/var/nfs/shared/daedalus`
read-write, reproducing the same paths the developer workstation sees.
NodeAffinity is set to `daedalus-02|03|04` for the app pod. Dynamo workers are
restricted to nodes with `nvidia.com/gpu.memory > 40959` and required
anti-affinity. The default deployment schedules two TP=2 workers on two
eligible nodes. See [`photo-tagger/README.md`](./photo-tagger/README.md) for
the full endpoint reference and configuration.

## File Layout

```text
qwen-36-27b-dynamo/
  README.md
  Dockerfile                        # Qwen SGLang image
  docker-compose.yaml               # local image build convenience
  deploy.sh                         # builds, pushes, applies the overlay
  test-endpoint.sh                  # smoke-test the frontend
  Makefile                          # `make validate` entry point
  scripts/
    validate.sh                     # shell/Python/kustomize/cargo checks
  observability/
    README.md                       # monitoring-stack prerequisites + install
    grafana/                        # Dynamo + SGLang Grafana dashboard
  k8s/
    base/                           # integrated Dynamo + Redis + Postgres + app
    overlays/
      daedalus/                     # NFS mounts, UI NodePort, Daedalus defaults
    benchmarks/
      aiperf/                       # opt-in router benchmark Job
      dynosim-aiperf/               # opt-in DynoSim + AIPerf comparison Job
  photo-tagger/                     # companion Rust app — image cataloging UI
    README.md
    Cargo.toml
    Dockerfile
    deploy.sh
    src/                            # axum + tokio + Postgres + image
    static/index.html
```
