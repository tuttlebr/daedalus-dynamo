# Daedalus Dynamo Gateway Deployment

This repository contains three NVIDIA Dynamo model backends behind one shared
Gateway API Inference Extension deployment path. The repository-root
[`deploy.sh`](./deploy.sh) is the primary entrypoint for building, pushing,
rendering, applying, and deleting the shared deployment.

The default demo is the text-only
[`routing-workbench/`](./routing-workbench/) app. It exercises OpenAI-compatible
text requests through GAIE: `/v1/models`, model-header routing, EPP selection,
KV-aware routing, worker scaling, streaming TTFT, latency, and optional Dynamo
`nvext.extra_fields` such as `worker_id` and `timing`.

| Backend name | Folder | Model | Runtime |
| --- | --- | --- | --- |
| `nemotron` | `nemotron-3-nano-omni-dynamo/` | `nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-FP8` | vLLM |
| `qwen35b` | `qwen-36-35b-dynamo/` | `Qwen/Qwen3.6-35B-A3B-FP8` | vLLM |
| `qwen27b` | `qwen-36-27b-dynamo/` | `Qwen/Qwen3.6-27B-FP8` | SGLang |

Shared Grafana dashboard assets live in [`observability/`](./observability/).
Example-level `deploy.sh` files are compatibility wrappers only.

## Cluster prerequisites

Each backend serves a TP=2 worker pool on high-memory GPU nodes behind the
shared gateway. Before deploying, confirm the cluster provides:

| Requirement | Provides | Notes |
| --- | --- | --- |
| `kubectl` + reachable cluster | API access | A current context must resolve and respond. |
| Dynamo + Grove operators | DynamoGraphDeployment / Grove PodCliqueSet reconciliation | Required; the deployment renders a `DynamoGraphDeployment` per backend. |
| Gateway API + GAIE + agentgateway CRDs | Gateway, HTTPRoute, InferencePool, EPP | Install or upgrade with `INSTALL_GATEWAY=1`. |
| NVIDIA GPU Operator | `nvidia.com/gpu` scheduling, `nvidia.com/gpu.memory` labels | Per-node GPU memory labels gate worker placement. |
| At least `WORKER_REPLICAS` high-memory GPU nodes | Worker capacity (TP=2 each) | Default `WORKER_REPLICAS=2` needs two eligible nodes per backend. |
| A default StorageClass **and** an RWX StorageClass | Model-cache PVC | The model-cache PVC is `100Gi` `ReadWriteMany`. |
| NVIDIA DCGM exporter | GPU telemetry | Optional; only for the shared Grafana dashboard. |
| NATS service | Event plane | Optional; the frontend uses ZMQ and does not require NATS. |
| RDMA/InfiniBand + NVIDIA Network Operator | GPUDirect RDMA fabric | Optional; only needed for `TRANSPORT_PROFILE=rdma` / `SERVING_MODE=disagg`. |

## Step 0: check your cluster

Run the read-only cluster doctor before deploying to verify the requirements
above:

```bash
./deploy.sh doctor
```

The same checks are runnable directly as `scripts/cluster-doctor.sh`. The doctor
prints `[OK]`/`[WARN]`/`[FAIL]` lines plus a final verdict. It exits non-zero
only when something `FAIL`ed. A hard `FAIL` is reserved for missing CRDs,
operators, GPUs, or a missing default StorageClass; missing RDMA/InfiniBand is
always a `WARN`, never a `FAIL`, because aggregated serving still works without
it. `DOCTOR_JSON=1 ./deploy.sh doctor` emits JSON instead of lines.

To gate the deploy on the doctor, run it as part of `deploy`:

```bash
RUN_DOCTOR=1 HF_TOKEN=<hugging-face-token> ./deploy.sh deploy
```

`RUN_DOCTOR=1` runs the doctor first as advisory; add `DOCTOR_STRICT=1` to abort
the deploy on any doctor `FAIL`.

## Deploy

With no backend argument, the root script deploys the default `qwen35b` demo
with `WORKER_REPLICAS=2`, `EPP_ENABLE_DYN_DECODE=1`, and the Routing Workbench
enabled:

```bash
HF_TOKEN=<hugging-face-token> ./deploy.sh deploy
```

Deploy specific backends explicitly:

```bash
HF_TOKEN=<hugging-face-token> ./deploy.sh deploy nemotron
HF_TOKEN=<hugging-face-token> ./deploy.sh deploy qwen35b qwen27b
HF_TOKEN=<hugging-face-token> ./deploy.sh deploy all
```

Backends may also be selected with `BACKENDS=nemotron,qwen35b`.

Use `INSTALL_GATEWAY=1` on first setup or when upgrading Gateway API, Gateway
API Inference Extension, or agentgateway:

```bash
INSTALL_GATEWAY=1 HF_TOKEN=<hugging-face-token> ./deploy.sh deploy
```

## Common Commands

```bash
./deploy.sh render
./deploy.sh render nemotron qwen35b
DEPLOY_WORKBENCH=0 ./deploy.sh render qwen35b
./deploy.sh delete qwen27b
DELETE_DATA=1 ./deploy.sh delete all
kubectl port-forward -n daedalus-dynamo svc/inference-gateway 8000:80
```

The Routing Workbench is exposed through NodePort `30153` by default:

```text
http://<any-node-ip>:30153/
```

## Workbench Credentials

The workbench uses HTTP Basic auth by default in Kubernetes. The username comes
from the `routing-workbench-config` ConfigMap, and the password is the token in
the `routing-workbench-auth` Secret.

Retrieve the current browser login:

```bash
NAMESPACE=${NAMESPACE:-daedalus-dynamo}

kubectl -n "${NAMESPACE}" get configmap routing-workbench-config \
  -o jsonpath='{.data.WORKBENCH_AUTH_USER}{"\n"}'

kubectl -n "${NAMESPACE}" get secret routing-workbench-auth \
  -o jsonpath='{.data.token}' | base64 -d
echo
```

API clients may also send the token as a bearer token:

```bash
WORKBENCH_TOKEN="$(
  kubectl -n "${NAMESPACE:-daedalus-dynamo}" get secret routing-workbench-auth \
    -o jsonpath='{.data.token}' | base64 -d
)"

curl -H "Authorization: Bearer ${WORKBENCH_TOKEN}" \
  http://<any-node-ip>:30153/api/status
```

To choose credentials at first deploy:

```bash
WORKBENCH_AUTH_USER=<username> \
WORKBENCH_AUTH_TOKEN=<strong-token> \
HF_TOKEN=<hugging-face-token> \
./deploy.sh deploy
```

If `routing-workbench-auth` already exists, `deploy.sh` preserves it.

## Configuration

Important root-level environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `NAMESPACE` | `daedalus-dynamo` | Shared Kubernetes namespace. |
| `REGISTRY` | `daedalus.ddns.me:5050/btuttle` | Image registry prefix. |
| `IMAGE_TAG` | UTC timestamp | Immutable image tag for model and workbench images. |
| `SKIP_BUILD` / `SKIP_PUSH` / `SKIP_DEPLOY` | unset | Skip build, push, or apply phases. |
| `DEPLOY_WORKBENCH` | `1` | Deploy the shared Routing Workbench; set `0` for gateway/backends only. |
| `DEPLOY_PHOTO_TAGGER` | unset | Compatibility alias: `DEPLOY_PHOTO_TAGGER=0` disables the shared demo app. |
| `WORKBENCH_IMAGE_REPOSITORY` | `${REGISTRY}/routing-workbench` | Workbench image repository. |
| `WORKBENCH_NODEPORT` | `30153` | Workbench NodePort. |
| `OPENAI_BASE_URL` | `http://inference-gateway:80/v1` | Workbench OpenAI-compatible base URL. |
| `DEFAULT_BACKEND` | first selected backend | Backend used to derive the default workbench model. |
| `WORKBENCH_DEFAULT_MODEL` | backend model id | Workbench model override. |
| `WORKBENCH_REQUIRE_AUTH` | `true` in Kubernetes | Require auth for UI/API routes. |
| `WORKBENCH_AUTH_USER` | `routing-workbench` | HTTP Basic username. |
| `WORKBENCH_AUTH_TOKEN` | generated secret | HTTP Basic password or bearer token. |
| `WORKER_REPLICAS` | `2` | Worker replicas for each selected backend. |
| `DYNAMO_EPP_IMAGE` | `nvcr.io/nvidia/ai-dynamo/dynamo-frontend:${DYNAMO_VERSION:-1.2.0}` | EPP container image. |
| `EPP_ENABLE_DYN_DECODE` | `1` | Enables Dynamo KV-aware EPP scoring. |
| `EPP_STAGE_UCX_LIBS` | `1` | Stages UCX runtime libraries from the selected backend image into the EPP pod. |
| `EPP_NODE_HOSTNAMES` | unset | Optional comma-separated hostname allow-list for EPP pods. |
| `KV_CACHE_BLOCK_SIZE` | `16` | Worker block/page size env value; runtimes may adjust effective size. |
| `TRANSPORT_PROFILE` | `tcp` | Worker KV transport: `tcp` (default) or `rdma`. |
| `RDMA_UCX_NET_DEVICES` | `mlx5_0:1` | UCX `HCA:port` for the `rdma` profile. |
| `RDMA_NCCL_IB_HCA` | `mlx5_0,mlx5_1` | NCCL IB HCAs for the `rdma` profile. |
| `RDMA_NCCL_SOCKET_IFNAME` | `eth0` | NCCL bootstrap NIC for the `rdma` profile. |
| `RDMA_RESOURCE_NAME` | unset | RDMA device-plugin resource to request when set under the `rdma` profile, e.g. `rdma/rdma_shared_device_a`. |
| `SERVING_MODE` | `agg` | `agg` (default) or `disagg` (prefill/decode split); `disagg` requires `TRANSPORT_PROFILE=rdma`. |
| `RUN_DOCTOR` | `0` | Run the cluster doctor before deploy (advisory). |
| `DOCTOR_STRICT` | `0` | With `RUN_DOCTOR=1`, abort the deploy on any doctor `FAIL`. |
| `DOCTOR_RUN_NODE_PROBE` | `auto` | Node-level RDMA probe mode: `auto` (exec a running worker pod, else print the opt-in command), `job` (schedule a short-lived probe pod and clean it up), or `off`. |
| `DOCTOR_PROBE_IMAGE` | unset | Image used by the `DOCTOR_RUN_NODE_PROBE=job` probe pod. |
| `DOCTOR_JSON` | unset | `DOCTOR_JSON=1` emits doctor results as JSON. |

Qwen compatibility wrappers map old `QWEN_IMAGE_REPOSITORY` values to
`QWEN35B_IMAGE_REPOSITORY` or `QWEN27B_IMAGE_REPOSITORY`. New automation should
use backend-specific variables directly.

### Transport profiles, RDMA/InfiniBand & disaggregated serving

The default `TRANSPORT_PROFILE=tcp` keeps today's behavior exactly: worker
NIXL/UCX transport over TCP, which works on any cluster. `TRANSPORT_PROFILE=rdma`
enables InfiniBand/RoCE + GPUDirect RDMA on the workers (UCX `rc`/`ib` with
`UCX_IB_GPU_DIRECT_RDMA=1`) and the NCCL IB env, tuned with the `RDMA_*`
variables above.

`SERVING_MODE=disagg` splits each backend into separate prefill and decode
worker pools that move KV cache over NIXL. It **requires**
`TRANSPORT_PROFILE=rdma` (`deploy.sh` errors otherwise) and roughly doubles the
GPU requirement (a prefill pool plus a decode pool).

Because both depend on a working fabric, validate the cluster with the doctor
first and confirm its RDMA/InfiniBand checks pass before enabling them:

```bash
./deploy.sh doctor
TRANSPORT_PROFILE=rdma SERVING_MODE=disagg HF_TOKEN=<hugging-face-token> ./deploy.sh deploy qwen35b
```

## GAIE Topology

The root deployment creates:

- `Gateway` named `inference-gateway` using the `agentgateway` GatewayClass.
- `AgentgatewayPolicy` that extracts the OpenAI request body `model` field into
  `X-Gateway-Base-Model-Name` before routing.
- `HTTPRoute` that sends `/v1/models` to the generated model catalog and sends
  `/v1/*` traffic to the backend `InferencePool` whose model header matches.
- One `DynamoGraphDeployment` per selected backend.
- One operator-generated `InferencePool` and EPP per selected backend.
- `WORKER_REPLICAS` worker-side Dynamo frontend sidecars per backend, running
  `python3 -m dynamo.frontend --router-mode direct` so EPP routing decisions are
  honored.
- One shared Routing Workbench deployment when `DEPLOY_WORKBENCH=1`.

The EPP config uses the Dynamo GAIE plugin chain:

- `disagg-profile-handler`
- `label-filter` scoped to decode workers
- `dyn-decode-scorer` when `EPP_ENABLE_DYN_DECODE=1`
- `max-score-picker`

NVIDIA's GAIE documentation describes this integration as gateway-layer,
KV-aware request routing and notes that the worker frontend must run in direct
router mode so it respects EPP worker selections:
<https://docs.nvidia.com/dynamo/integrations/kubernetes-integrations/gateway-api-inference-extension-gaie>.

## Developer Notes

Keep deployment behavior centralized in the root [`deploy.sh`](./deploy.sh).
Do not add model-specific Kubernetes apply logic back into example-level
`deploy.sh` files.

The workbench calls the shared gateway like any other OpenAI-compatible client:

- Model catalog: `http://inference-gateway:80/v1/models`
- Chat completions: `http://inference-gateway:80/v1/chat/completions`
- Routing key: the OpenAI request body `model` field

To integrate a different application, deploy the backends with
`DEPLOY_WORKBENCH=0`, deploy your app in the same namespace, and configure its
OpenAI base URL as `http://inference-gateway:80/v1`.

Recommended checks:

```bash
bash -n deploy.sh scripts/*.sh scripts/pre-commit/*.sh
cargo fmt --manifest-path routing-workbench/Cargo.toml -- --check
cargo test --manifest-path routing-workbench/Cargo.toml
IMAGE_TAG=test ./deploy.sh render qwen35b \
  | kubectl apply --dry-run=client -f -
```

For a live smoke test:

```bash
curl -sS http://<workbench-address>/api/status
curl -sS http://<workbench-address>/api/runs \
  -H 'Content-Type: application/json' \
  -d '{"preset":"repeated-shared-prefix","total_requests":2,"concurrency":2,"max_tokens":16}'
```

## Security

Do not commit Hugging Face tokens, rendered Secrets, or local `.env` files.
Keep NodePorts and any LoadBalancer address behind trusted networks. PVCs and
app secrets are preserved on delete unless `DELETE_DATA=1` is set.
