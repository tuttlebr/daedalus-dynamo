# Daedalus Dynamo Helm Deployments

This repository deploys NVIDIA Dynamo model backends with one reusable Helm
chart and model-specific values files. The root [`Makefile`](./Makefile) is the
primary entrypoint for building images, rendering charts, deploying releases,
and uninstalling releases by model.

| Model target | Values file | Model | Runtime |
| --- | --- | --- | --- |
| `nemotron-omni` | `values/nemotron-omni.yaml` | `nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-NVFP4` | vLLM |
| `qwen35b` | `values/qwen35b.yaml` | `nvidia/Qwen3.6-35B-A3B-NVFP4` | vLLM |
| `qwen27b` | `values/qwen27b.yaml` | `nvidia/Qwen3.6-27B-NVFP4` | vLLM |
| `gemma431b` | `values/gemma431b.yaml` | `nvidia/Gemma-4-31B-IT-NVFP4` | vLLM |
| `nemotronnano` | `values/nemotronnano.yaml` | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4` | vLLM |

`MODEL=nemotron` and `MODEL=omni` are accepted aliases for
`MODEL=nemotron-omni`.

## Prerequisites

The deployment host needs:

- Bash, GNU Make, Docker, Helm, `kubectl`, and Python 3
- a working `kubectl` context with cluster-scoped CRD and Helm permissions
- push access to the configured image registry, with every GPU and gateway node
  able to pull from that registry
- network access to GitHub release assets and the agentgateway OCI registry
- a Hugging Face token that can read the selected model

The cluster must already have the Dynamo and Grove operators, NVIDIA GPU
support, a default StorageClass, and an RWX-capable StorageClass for model
cache PVCs. `make install-gateway` does not install those dependencies. The
Prometheus Operator is optional; without its CRDs, the gateway metrics Service
is created but its ServiceMonitor and alert rules are skipped.

Helm 3.17 or newer is required only when using `TAKE_OWNERSHIP=1` to migrate
existing resources into a Helm release.

## First-Time Installation

All commands below are run from the repository root. Confirm the intended
cluster before installing cluster-scoped APIs:

```bash
kubectl config current-context
make models
```

### 1. Configure Image Repositories

Checked-in values use the Daedalus registry. Authenticate Docker before the
first build and ensure the Kubernetes nodes are configured to pull from it:

```bash
docker login daedalus.ddns.me:5050
```

To use another registry, export both full image references. Keep these exports
in the shell for later `make` commands because every gateway apply reconciles
the image stored in `AgentgatewayParameters`:

```bash
export GATEWAY_IMAGE=registry.example.com/daedalus/agentgateway:v1.3.1
export IMAGE=registry.example.com/daedalus/dynamo-vllm-runtime:1.3.0-dev.1
docker login registry.example.com
```

Both references must include a registry, repository, and tag. For a private
registry, configure node-level pull credentials before deployment; the chart
does not create registry credentials.

### 2. Install the Inference Gateway

Install or upgrade Gateway API, Gateway API Inference Extension, agentgateway,
and the shared hardened gateway:

```bash
make install-gateway
```

This is an idempotent cluster setup step, not a per-model requirement. It
builds and pushes `GATEWAY_IMAGE`, installs the control plane, then applies the
gateway base and monitoring resources. Once the same gateway image is already
published, reuse it with:

```bash
SKIP_GATEWAY_BUILD=1 SKIP_GATEWAY_PUSH=1 make install-gateway
```

`INSTALL_GATEWAY=1 make deploy-<model>` can combine this step with a model
deployment, but the explicit first-time sequence makes cluster and registry
failures easier to isolate.

### 3. Run the Cluster Preflight

The doctor is read-only and verifies operators, storage, GPU capacity, gateway
replicas, API compatibility, and live process FD limits:

```bash
make doctor
```

The Helm model chart owns model-local resources: model cache PVC, Redis, direct
`DynamoGraphDeployment` or `DynamoGraphDeploymentRequest`, Planner runtime
compatibility settings, and optional PodMonitor. The Makefile deploy flow also
applies the shared `inference-gateway`, model-header policy, `/v1/models`
catalog, and the selected model's HTTPRoute.

## Deploy A Model

The default model is `qwen35b`:

```bash
HF_TOKEN=<hugging-face-token> make deploy
```

Deploy a specific model either with `MODEL=` or a pattern target:

```bash
HF_TOKEN=<hugging-face-token> make deploy MODEL=qwen35b
HF_TOKEN=<hugging-face-token> make deploy-qwen27b
HF_TOKEN=<hugging-face-token> make deploy-gemma431b
HF_TOKEN=<hugging-face-token> make deploy-nemotron
HF_TOKEN=<hugging-face-token> make deploy-nemotronnano
```

`make deploy` builds the image, pushes it, creates or reuses
`hf-token-secret`, applies the shared inference gateway resources, runs
`helm upgrade --install`, then refreshes the selected model route and model
catalog. To deploy an already pushed image:

```bash
SKIP_BUILD=1 SKIP_PUSH=1 HF_TOKEN=<hugging-face-token> make deploy-qwen35b
```

The explicit `make install-gateway` step must have succeeded at least once.
Normal model deployments reconcile gateway resources but do not reinstall its
cluster-scoped CRDs or control plane.

For an existing release whose resources were created outside Helm, use Helm
3.17+ ownership adoption:

```bash
TAKE_OWNERSHIP=1 SKIP_BUILD=1 SKIP_PUSH=1 HF_TOKEN=<hugging-face-token> make deploy-qwen35b
```

Check the release and endpoint after deployment:

```bash
make status-qwen35b
kubectl get dgd,pods,httproute -n daedalus-dynamo
kubectl get svc inference-gateway -n daedalus-dynamo
curl --fail http://192.168.1.234:32359/v1/models
```

Use the gateway address reported by the Service when it differs from the
Daedalus endpoint. For local access without a reachable LoadBalancer or
NodePort:

```bash
kubectl port-forward -n daedalus-dynamo svc/inference-gateway 8000:32359
curl --fail http://127.0.0.1:8000/v1/models
```

## Common Commands

```bash
make models
make install-gateway
make lint-qwen35b
make render-qwen35b
make dry-run-qwen35b
make gateway-qwen35b
make upgrade-qwen35b
make status-qwen35b
make uninstall-qwen35b
make lint-all
make deploy-all
make uninstall-all
```

`make upgrade` runs only Helm. `make deploy` is the full image plus Helm flow.
Use `make upgrade-<model>` for chart-only changes that leave the model identity,
route, secret, and gateway unchanged. When `model.name`, `model.id`, the
namespace, or gateway settings change, use the full deploy path with
`SKIP_BUILD=1 SKIP_PUSH=1`; it also refreshes the HTTPRoute and `/v1/models`
catalog.

`make uninstall` removes the Helm release, deletes that model's HTTPRoute, and
refreshes the catalog. Chart-managed PVCs are annotated with
`helm.sh/resource-policy: keep`, so model cache and Redis data claims remain
after Helm uninstall.

## Gateway Resilience

`make install-gateway` builds and pushes the hardened image from
[`Dockerfile.agentgateway`](./Dockerfile.agentgateway), installs a compatible
Gateway API and agentgateway control plane, and applies the shared gateway. The
gateway defaults are sized for sustained benchmark traffic:

- `RLIMIT_NOFILE` soft limit `131072`, verified before the proxy starts
- two proxy and two controller replicas, with proxy topology spreading across nodes
- proxy HPA range `2-8` and controller HPA range `2-4`
- PodDisruptionBudgets and zero-unavailable rolling proxy updates
- upstream idle pool capped at 64 connections per host with a 30-second idle timeout
- 60-second downstream HTTP/1 idle timeout and graceful connection rotation
- per-pod open FD, FD headroom, and TCP socket-state metrics

The proxy wrapper raises only the soft limit. It fails fast if the container
runtime hard limit is below `GATEWAY_NOFILE_LIMIT`, so a deployment cannot
silently return with the original 1024-descriptor ceiling.

Check the live resilience gates before benchmarking:

```bash
DOCTOR_RUN_NODE_PROBE=off make doctor
kubectl get deploy,hpa,pdb -n daedalus-dynamo \
  -l gateway.networking.k8s.io/gateway-name=inference-gateway
```

Run the sustained FD regression after the endpoint is ready. Its defaults
match the benchmarking suite's interactive-chat shape: about 900 input tokens,
150 output tokens, concurrency 64, and five minutes of traffic.

```bash
scripts/test-endpoint-fd-stability.py \
  --url http://192.168.1.234:32359 \
  --duration 300 \
  --cooldown 90 \
  --output artifacts/fd-stability.json
```

The regression fails on any `EMFILE` response, an excessive request error
rate, FD utilization above 80 percent, missing telemetry, or FD counts that do
not return close to the per-pod baseline after cooldown.

## Images

By default, the Makefile reads `model.image` from the selected values file and
uses that repository and tag for both Docker and Helm. Override the tag or the
whole image when needed:

```bash
IMAGE_TAG=test make build-qwen35b
IMAGE=registry.example.com/dynamo/qwen35b:test make deploy-qwen35b
BASE_IMAGE=nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.3.0-dev.1-cuda13 make build-qwen35b
```

The root [`Dockerfile`](./Dockerfile) builds the shared vLLM runtime image used
by the model chart.

## Helm Values

Model-specific settings live in [`values/`](./values). Shared defaults live in
[`charts/dynamo-model/values.yaml`](./charts/dynamo-model/values.yaml).

Configuration is resolved in this order:

1. Shared chart defaults in `charts/dynamo-model/values.yaml`.
2. The selected `values/<model>.yaml` file.
3. Makefile-controlled image, secret, and optional deployment-mode overrides.
4. Additional `HELM_ARGS`, which take final precedence.

Use a model values file for durable model identity, worker arguments, resource
sizing, Planner policy, and model-specific environment variables. Use Makefile
variables for one deployment invocation, and reserve `HELM_ARGS` for deliberate
Helm overrides that do not yet belong in a checked-in values file.

Render direct DGD mode, which is the checked-in default:

```bash
make render-qwen35b
```

Render the DGDR source object instead:

```bash
DEPLOYMENT_MODE=dgdr make render-qwen35b
```

Pass arbitrary Helm overrides through `HELM_ARGS`:

```bash
HELM_ARGS='--set planner.advisory=true --set planner.maxGpuBudget=2' \
  SKIP_BUILD=1 SKIP_PUSH=1 HF_TOKEN=<hugging-face-token> make deploy-qwen35b
```

Validate configuration before applying it:

```bash
make lint-qwen35b
make dry-run-qwen35b
```

To add a model, copy an existing file under `values/`, change at least
`model.name`, `model.id`, `model.image`, and the worker arguments, then run
`make lint MODEL=<new-file-basename>`. The Makefile discovers values files
automatically; no per-model deployment script is needed.

Useful Makefile variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `MODEL` | `qwen35b` | Model values file basename. |
| `RELEASE` | selected model | Helm release name. |
| `NAMESPACE` | `daedalus-dynamo` | Kubernetes namespace. |
| `IMAGE` | selected `model.image` | Full runtime image reference. |
| `IMAGE_TAG` | tag from selected `model.image` | Runtime image tag override. |
| `BASE_IMAGE` | Dynamo vLLM CUDA 13 runtime | Docker build base image. |
| `DEPLOYMENT_MODE` | values file setting | Optional `direct` or `dgdr` override. |
| `TAKE_OWNERSHIP` | `0` | Pass Helm `--take-ownership`. |
| `SKIP_BUILD` / `SKIP_PUSH` | `0` | Skip image steps during `make deploy`. |
| `SKIP_SECRET` | `0` | Skip HF token secret check/update during `make deploy`. |
| `GATEWAY` | `1` | Apply shared inference gateway resources, model route, and model catalog during `make deploy`. |
| `INSTALL_GATEWAY` | `0` | Install or upgrade Gateway API, GAIE, and agentgateway before applying gateway resources. |
| `GATEWAY_IMAGE` | internal `daedalus-agentgateway:v1.3.1` | Hardened agentgateway image built by `make gateway-image`. |
| `GATEWAY_REPLICAS` | `2` | Static proxy replica floor before HPA reconciliation. |
| `GATEWAY_NOFILE_LIMIT` | `131072` | Verified proxy `RLIMIT_NOFILE` soft limit. |
| `GATEWAY_HPA_MIN_REPLICAS` / `GATEWAY_HPA_MAX_REPLICAS` | `2` / `8` | Proxy HPA range. |
| `GATEWAY_POOL_MAX_SIZE` | `64` | Maximum idle upstream connections retained per host. |
| `SKIP_GATEWAY_BUILD` / `SKIP_GATEWAY_PUSH` | `0` | Reuse an already published hardened gateway image. |
| `HF_TOKEN_SECRET` | `hf-token-secret` | Secret referenced by Dynamo services. |
| `HELM_ARGS` | unset | Extra args for lint, render, dry-run, and upgrade. |

`planner.workerNamespaceCompat=legacy` is the chart default. It keeps Planner
forward-pass metric discovery on the base worker namespace used by the current
operator/runtime stack. Set it to `hashed` only when the worker runtime actually
publishes metadata under the DGD worker-hash namespace.

## Developer Notes

Keep deployment behavior centralized in the root Makefile and model behavior in
the Helm chart plus `values/*.yaml`. Avoid adding model-specific deploy scripts
or Kubernetes YAML branches.

Recommended checks:

```bash
make lint-all
make validate-resilience
make render-qwen35b | kubectl apply --dry-run=client -f -
```

## Security

Do not commit Hugging Face tokens, rendered Secrets, or local `.env` files.
Keep NodePorts and any LoadBalancer address behind trusted networks. PVCs and
app secrets are preserved unless explicitly deleted.
