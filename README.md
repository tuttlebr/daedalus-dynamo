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

Qwen compatibility wrappers map old `QWEN_IMAGE_REPOSITORY` values to
`QWEN35B_IMAGE_REPOSITORY` or `QWEN27B_IMAGE_REPOSITORY`. New automation should
use backend-specific variables directly.

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
