# Daedalus Dynamo Gateway Deployment

This repository contains three NVIDIA Dynamo model backends and one shared
Gateway API Inference Extension deployment path. The repository-root
[`deploy.sh`](./deploy.sh) is the primary deployment entrypoint. It builds,
pushes, renders, applies, and deletes the shared deployment for one or more
selected model backends.

The example-level `deploy.sh` files are compatibility wrappers only. They do
not contain separate Kubernetes configuration.

| Backend name | Folder | Model | Runtime |
| --- | --- | --- | --- |
| `nemotron` | `nemotron-3-nano-omni-dynamo/` | `nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-FP8` | vLLM |
| `qwen35b` | `qwen-36-35b-dynamo/` | `Qwen/Qwen3.6-35B-A3B-FP8` | vLLM |
| `qwen27b` | `qwen-36-27b-dynamo/` | `Qwen/Qwen3.6-27B-FP8` | SGLang |

Shared Grafana dashboard assets live in [`observability/`](./observability/).
The shared reference application lives once at [`photo-tagger/`](./photo-tagger/);
backend folders no longer contain their own app copies.

## Deploy

Deploy one or more backends from the repository root:

```bash
HF_TOKEN=<hugging-face-token> ./deploy.sh deploy nemotron
HF_TOKEN=<hugging-face-token> ./deploy.sh deploy qwen35b qwen27b
HF_TOKEN=<hugging-face-token> ./deploy.sh deploy all
```

Backends may also be selected with `BACKENDS=nemotron,qwen35b`. With no backend
argument, the root script defaults to `all`.

Use `INSTALL_GATEWAY=1` on first setup or when upgrading Gateway API, Gateway
API Inference Extension, or agentgateway:

```bash
INSTALL_GATEWAY=1 HF_TOKEN=<hugging-face-token> ./deploy.sh deploy all
```

The installer currently applies:

- Gateway API `${GATEWAY_API_VERSION:-v1.5.1}`
- Gateway API Inference Extension `${GAIE_VERSION:-v1.5.0}`
- agentgateway `${AGENTGATEWAY_VERSION:-v1.2.1}`

The upstream NVIDIA Dynamo GAIE reference is here:
<https://raw.githubusercontent.com/ai-dynamo/dynamo/refs/heads/main/docs/kubernetes/inference-gateway.md>.

## Common Commands

```bash
./deploy.sh render nemotron qwen35b
./deploy.sh delete qwen27b
DELETE_DATA=1 ./deploy.sh delete all
kubectl port-forward -n daedalus-dynamo svc/inference-gateway 8000:80
DEPLOY_PHOTO_TAGGER=0 ./deploy.sh render all
```

The shared Photo Tagger UI is exposed through NodePort `30153` by default:

```text
http://<any-node-ip>:30153/
```

## Photo Tagger Credentials

Photo Tagger uses HTTP Basic auth by default. The username comes from the
`photo-tagger-config` ConfigMap, and the password is the token stored in the
`photo-tagger-auth` Secret.

Retrieve the current browser login:

```bash
NAMESPACE=${NAMESPACE:-daedalus-dynamo}

kubectl -n "${NAMESPACE}" get configmap photo-tagger-config \
  -o jsonpath='{.data.PHOTO_TAGGER_AUTH_USER}{"\n"}'

kubectl -n "${NAMESPACE}" get secret photo-tagger-auth \
  -o jsonpath='{.data.token}' | base64 -d
echo
```

Or export both values for API calls:

```bash
PHOTO_TAGGER_USER="$(
  kubectl -n "${NAMESPACE:-daedalus-dynamo}" get configmap photo-tagger-config \
    -o jsonpath='{.data.PHOTO_TAGGER_AUTH_USER}'
)"
PHOTO_TAGGER_TOKEN="$(
  kubectl -n "${NAMESPACE:-daedalus-dynamo}" get secret photo-tagger-auth \
    -o jsonpath='{.data.token}' | base64 -d
)"
```

Use `PHOTO_TAGGER_USER` as the browser username and `PHOTO_TAGGER_TOKEN` as the
browser password. API clients may also send the token as a bearer token:

```bash
curl -H "Authorization: Bearer ${PHOTO_TAGGER_TOKEN}" \
  http://<any-node-ip>:30153/health
```

To choose credentials at first deploy:

```bash
PHOTO_TAGGER_AUTH_USER=<username> \
PHOTO_TAGGER_AUTH_TOKEN=<strong-token> \
HF_TOKEN=<hugging-face-token> \
./deploy.sh deploy nemotron
```

If `photo-tagger-auth` already exists, `deploy.sh` preserves it. To rotate the
token, update the Secret and restart Photo Tagger:

```bash
NAMESPACE=${NAMESPACE:-daedalus-dynamo}
kubectl -n "${NAMESPACE}" create secret generic photo-tagger-auth \
  --from-literal=token="$(openssl rand -base64 32)" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "${NAMESPACE}" rollout restart deployment/photo-tagger
```

The example wrappers still work for muscle memory:

```bash
cd nemotron-3-nano-omni-dynamo
HF_TOKEN=<hugging-face-token> ./deploy.sh deploy
```

That command forwards to `../deploy.sh deploy nemotron`.

## Configuration

Important root-level environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `NAMESPACE` | `daedalus-dynamo` | Shared Kubernetes namespace. |
| `REGISTRY` | `daedalus.ddns.me:5050/btuttle` | Image registry prefix. |
| `IMAGE_TAG` | UTC timestamp | Immutable image tag for model and reference app images. |
| `SKIP_BUILD` / `SKIP_PUSH` / `SKIP_DEPLOY` | unset | Skip build, push, or apply phases. |
| `DEPLOY_PHOTO_TAGGER` | `1` | Deploy the shared reference app; set `0` for gateway/backends only. |
| `DEFAULT_BACKEND` | first selected backend | Photo Tagger default backend. |
| `DEFAULT_MODEL` | backend model id | Photo Tagger model override. |
| `DYNAMO_EPP_IMAGE` | `nvcr.io/nvidia/ai-dynamo/dynamo-frontend:${DYNAMO_VERSION:-1.2.0}` | EPP container image. |
| `EPP_ENABLE_DYN_DECODE` | `1` | Enables Dynamo KV-aware EPP scoring. |
| `EPP_STAGE_UCX_LIBS` | `1` | Stages UCX runtime libraries from the selected backend image into the EPP pod. |
| `EPP_NODE_HOSTNAMES` | unset | Optional comma-separated hostname allow-list for EPP pods. |
| `KV_CACHE_BLOCK_SIZE` | `16` | Worker block/page size env value; some runtimes may adjust effective size. |

Qwen compatibility wrappers map old `QWEN_IMAGE_REPOSITORY` values to the
backend-specific root variables `QWEN35B_IMAGE_REPOSITORY` or
`QWEN27B_IMAGE_REPOSITORY`. New automation should use the backend-specific
variables directly.

## GAIE Topology

The root deployment creates one shared frontend:

- `Gateway` named `inference-gateway` using the `agentgateway` GatewayClass.
- `AgentgatewayPolicy` that extracts the OpenAI-compatible JSON request
  `model` field into `X-Gateway-Base-Model-Name` before routing.
- `HTTPRoute` that sends `/v1/models` to the generated model catalog and sends
  `/v1/*` traffic to the backend `InferencePool` whose model header matches.
- One `DynamoGraphDeployment` per selected backend.
- One operator-generated `InferencePool` and EPP per selected backend.
- One worker-side Dynamo frontend sidecar per worker, running
  `python3 -m dynamo.frontend --router-mode direct` so the EPP routing decision
  is honored.
- When `DEPLOY_PHOTO_TAGGER=1`, one shared Photo Tagger deployment configured
  with `LLM_ENDPOINT=http://inference-gateway:80/v1/chat/completions`.

The EPP config uses the Dynamo GAIE plugin chain:

- `disagg-profile-handler`
- `label-filter` scoped to decode workers
- `dyn-decode-scorer` when `EPP_ENABLE_DYN_DECODE=1`
- `max-score-picker`

The current `dynamo-frontend:1.2.0` image includes NIXL libraries but can miss
UCX runtime dependencies needed by `dyn-decode-scorer`. `EPP_STAGE_UCX_LIBS=1`
adds an init container that copies `nixl_cu*.libs` from the backend image into
an `emptyDir`, restores the EPP config volume, sets `NIXL_PLUGIN_DIR`, and
extends `LD_LIBRARY_PATH`.

## Developer Notes

Keep deployment behavior centralized in the root [`deploy.sh`](./deploy.sh).
Do not add model-specific Kubernetes apply logic back into example-level
`deploy.sh` files.

### Application Integration

Photo Tagger is a reference app, not a per-model component. It calls the shared
gateway like any other OpenAI-compatible client:

- Chat completions: `http://inference-gateway:80/v1/chat/completions`
- Model catalog: `http://inference-gateway:80/v1/models`
- Routing key: the OpenAI request body `model` field

To integrate a different application, deploy the backends with
`DEPLOY_PHOTO_TAGGER=0`, deploy your app in the same namespace, and configure its
OpenAI base URL as `http://inference-gateway:80/v1`. The app must choose one of
the model ids returned by `/v1/models`.

To add a backend:

1. Add the backend selector in `normalize_backend`.
2. Add model, DGD name, framework, worker component, storage prefix, image repo,
   build directory, and optional build arg helpers.
3. Add or reuse a worker renderer for the runtime.
4. Confirm `render_manifest` emits the model catalog, `HTTPRoute`, EPP,
   ModelExpress, Redis/PVCs, and worker service for the backend.
5. Validate with render and Kubernetes client dry-run.

Recommended checks:

```bash
bash -n deploy.sh scripts/install-inference-gateway.sh scripts/pre-commit/check-shell.sh
./scripts/pre-commit/check-shell.sh
IMAGE_TAG=test ./deploy.sh render nemotron qwen35b qwen27b \
  | kubectl apply --dry-run=client -f -
```

For a live smoke test:

```bash
curl -sS http://<gateway-address>/v1/models
curl -sS http://<gateway-address>/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-FP8","messages":[{"role":"user","content":"Reply with OK."}],"max_tokens":1}'
```

## Security

Do not commit Hugging Face tokens, rendered Secrets, or local `.env` files.
Keep the Photo Tagger NodePort and any LoadBalancer address behind trusted
networks. PVCs and app secrets are preserved on delete unless `DELETE_DATA=1`
is set.
