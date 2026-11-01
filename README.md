# Daedalus Dynamo

Portable single-node NVIDIA Dynamo deployment recipes for DGX Spark, L40S and A30.
The catalog snapshots all 93 entries in NVIDIA's inference-optimized checkpoint
collection and supplies 114 candidate recipes for 40 checkpoints, including one
specialized NVMe-offload recipe. These are starting configurations, not GPU-tested
performance guarantees.

See the [recipe guide and complete model matrix](docs/model-recipes.md),
[chart contract](charts/dynamo-model/README.md), and
[recorded collection decisions](config/model-catalog/recipes.json).

## Model organization

```text
values/
  _shared/vllm.yaml
  _hardware/{dgx-spark,l40s,a30}.yaml
  <builder>/<checkpoint>/model.yaml
  <builder>/<checkpoint>/<hardware>/tp1-pp1-dp1.yaml
  <builder>/<checkpoint>/l40s/tp2-pp1-dp1.yaml
```

Builder means the original family owner (NVIDIA, Qwen, Meta, Google, Microsoft,
Mistral); each model definition records the exact Hugging Face publisher ID.
A model definition inherits serving defaults. A recipe adds hardware, parallelism,
release identity and storage. Mappings merge; lists replace. `make` resolves the
layers before passing ordinary values to Helm.

All recipes inherit `worker.vllmOptions.max-model-len: auto`: vLLM determines the
largest context that fits its configured memory budget at startup, up to the
model's supported limit. Numeric `recipe.screeningContextLength` values are only
for offline memory screening and simulation; they do not cap serving context.

## Select and deploy

```bash
make models
make lint values/qwen/qwen3-8b-nvfp4/l40s/tp1-pp1-dp1.yaml
make render values/qwen/qwen3-8b-nvfp4/l40s/tp1-pp1-dp1.yaml
make deploy values/qwen/qwen3-8b-nvfp4/l40s/tp1-pp1-dp1.yaml
```

`deploy` installs the shared Model Express startup ConfigMap once per namespace,
checks the existing Hugging Face secret, and installs the selected model release.
Prebuilt runtime recipes skip local image builds. The special Qwen Flash Next
NVMe recipe builds its custom ARM64 image unless explicitly overridden.

`make deploy`, `make install`, and `make upgrade` route models through the shared
HTTP gateway on fixed port `32360` by default and refresh `/v1/models`.
The catalog polls each routed frontend at startup and every 30 seconds to publish
its runtime context limit as `context_window`, `max_model_len`, and
`context_length` (the same token count, covering input plus output). This picks
up the resolved limit when `max-model-len: auto` is used. Model IDs and reviewed
aliases stay intact; aliases report their canonical backend's limit. Limits are
omitted until the frontend reports them, and removed on the next refresh if the
frontend becomes unavailable or stops reporting the model. Offline fit estimates
and model-card maximums are not substituted for live values. When multiple
backends serve an ID, every backend must report a limit and the smallest is used.
Use `make gateway-catalog-apply` to refresh existing route configuration and
install catalog updates without redeploying model workers.

Use `GATEWAY=0` (or `recipe.gateway: false`) only for an explicitly private
deployment. To add a route for an already deployed model:

```bash
make gateway-route-apply values/qwen/qwen3-8b-nvfp4/l40s/tp1-pp1-dp1.yaml
make gateway-catalog-apply
```

Two-L40S recipes require two free GPUs on the **same node**. They request both
GPUs in one worker pod; PP, DP and worker replicas remain one, and Planner
scaling is disabled. No recipe combines GPUs across nodes.

For direct Helm usage:

```bash
python3 scripts/model_values.py resolve \
  values/qwen/qwen3-8b-nvfp4/l40s/tp1-pp1-dp1.yaml > /tmp/model-values.yaml
make runtime-install
helm upgrade --install my-model charts/dynamo-model \
  -n daedalus-dynamo -f /tmp/model-values.yaml
```

Use the recipe's `recipe.release` for subsequent operations, or consistently
supply `RELEASE=...`. Never install the same resolved resource names under two
Helm releases. Storage classes, namespaces, secrets and runtime images can be
overridden with the existing Make/Helm options.

## Legacy retirement

The previous flat/staging deployment profiles were removed at the user's request.
Frozen historical settings remain only under `tests/fixtures/retired-values/`
for regression tests and are excluded from `make models`. The stack-upgrade
script no longer redeploys the retired models. Retirement backups and live
teardown evidence are under `artifacts/portability-20261030/`.

## Reproduce the catalog and simulations

```bash
python3 scripts/build_model_catalog.py
python3 scripts/simulate_model_catalog.py
python3 -m unittest discover -s tests -p test_recipe_portability.py
python3 -m unittest discover -s tests -p test_model_cache_runtime.py
```

The generator uses the committed collection snapshot and compressed, hash-checked
HF configurations. The simulation runner uses a digest-pinned Planner 1.6.1 image,
CPU-only offline prediction, and records failures as well as estimates. See the
recipe guide for missing GB10/A30 coverage and estimator/runtime version differences.

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
export GATEWAY_IMAGE=registry.example.com/daedalus/agentgateway:v1.6.0
export IMAGE=registry.example.com/daedalus/dynamo-vllm-runtime:1.6.1
docker login registry.example.com
```

Both references must include a registry, repository, and tag. For a private
registry, configure node-level pull credentials before deployment; the chart
does not create registry credentials.

### 2. Install the Inference Gateway

The gateway uses the existing `daedalus/daedalus-tls` certificate Secret and
`daedalus-dynamo/inference-gateway-api-keys` authentication Secret. Provision
these before installation. The checked-in certificate grant permits the
`daedalus-dynamo` gateway to reference that certificate. The standard endpoint is
`http://192.168.1.12:32360/v1` (or any cluster node's LAN IP on port `32360`).
Both the Service port and NodePort are pinned to `32360`; Make and the renderer
reject conflicting port overrides. The controller-owned Service receives this
mapping from `AgentgatewayParameters`, so reconciliation and stack upgrades
retain it. All model routes share this gateway.

Existing verified HTTPS clients keep using `https://daedalus.ddns.me:32359/v1`.
Both public listeners require the existing bearer key. Switchyard continues to
use `http://inference-gateway-internal.daedalus-dynamo.svc.cluster.local:8000/v1`.
The internal listener remains exposed only through its ClusterIP Service.

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

`INSTALL_GATEWAY=1 make deploy <model-config.yaml>` can combine this step with a model
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
compatibility settings, PodDisruptionBudgets, and optional PodMonitor. Each
values file is an independently selected alternative; the sizing does not
assume that every model is deployed together. With gateway routing enabled,
the Makefile deploy flow also applies the shared `inference-gateway`, model-header
policy, `/v1/models` catalog, and the selected model's HTTPRoute.

## Agentic Gateway Performance Evaluation

Run the pinned multi-turn AIPerf workload against every model currently
advertised by the in-cluster gateway:

```bash
make benchmark-agentic-render
make benchmark-agentic
```

The render target reads the gateway Service's ClusterIP without changing the
cluster. The live target verifies
`EXPECTED_CONTEXT`, builds and pushes `AIPERF_IMAGE`, then creates one
non-retrying Job in `NAMESPACE`. The Job discovers models from
`http://daedalus.ddns.me:32360/v1/models`, using a pod-local hostname mapping
directly to the gateway Service's ClusterIP and `OPENAI_API_KEY` from Secret
`inference-gateway-api-keys`, key `switchyard`. The model list is not derived
from checked-in values files. Inference traffic stays inside the cluster and
does not use public DNS or the LAN VIP. `AIPERF_CONNECT_IP=auto` resolves the current ClusterIP on
each run. An explicit local IP selects a different path; an empty value opts
into normal DNS resolution.

When the benchmark completes or fails, its container waits for the target to
copy its run into
`artifacts/aiperf/<run-id>/workload/`. The adjacent `kubernetes/` directory
retains the submitted Job, runtime Job and Pod records, logs, repository state,
and workload checksums. The Job is deleted only after the workload artifacts
are local; it is retained until its deadline when copying fails. The ephemeral
evidence can be lost if the deadline expires or the pod is evicted. `artifacts/` is ignored by Git so
benchmark evidence remains available for local review without entering a
commit.

Reuse an image that is already available to the cluster with:

```bash
AIPERF_SKIP_BUILD=1 AIPERF_SKIP_PUSH=1 \
  AIPERF_IMAGE=registry.example.com/daedalus/aiperf:0.11.0 \
  make benchmark-agentic
```

The default workload is 128 five-turn agentic sessions at concurrency three,
or 640 requests per advertised model. Fresh sessions start throughout a
30-minute window, including a final session at 30 minutes; queued requests and
remaining turns finish afterward. The prompt shape targets about 70% reusable
tokens (a workload estimate, with actual KV hits dependent on the server).
The per-model safety timeout is two hours. It is live load, and its results
describe client-observed performance for that frozen workload only. See
[`benchmarks/aiperf/README.md`](./benchmarks/aiperf/README.md) for workload,
artifact, authentication, and interpretation details.

## Scheduling Recovery After Node Reboots

Grove-managed model pods use gang scheduling. A GPU pod can enter the terminal
`UnexpectedAdmissionError` phase when it is bound before the NVIDIA device
plugin reports healthy devices during a reboot. Older Grove releases retain
that failed pod, leaving the other pods in the gang `SchedulingGated` even
after the cluster becomes healthy.

Install the upstream gang-recovery fix, enable Grove health and priority
safeguards, reduce the recovery gate from four hours to 15 minutes, and wait
for all Dynamo pod gangs to recover:

```bash
make scheduling-resilience
```

The command is guarded to the expected Kubernetes context, upgrades Dynamo
and Grove together to the compatible `1.4.0` / `v0.1.0-alpha.12-rc1` pair,
reconciles Grove CRDs, and verifies the NVIDIA device plugin before making
changes. When a GPU worker explicitly selects the GB10 product label, it also
applies the worker's existing `nvidia.com/gpu=true:NoSchedule` toleration as a
reservation taint on GB10 nodes. Conflicting Deployment-owned GPU pods are
moved to general GPU capacity so they cannot claim the GB10 while the model is
recovering. Workloads that use general GPU affinity instead skip that optional
reservation step. After the device plugin is healthy, the command also removes
terminal `UnexpectedAdmissionError` pod records owned by restartable
controllers across the cluster. It preserves NATS and the externally managed
KAI scheduler. Override the guard or recovery window only when needed:

```bash
EXPECTED_CONTEXT=kubernetes-admin@kubernetes \
GROVE_TERMINATION_DELAY=15m \
make scheduling-resilience
```

Run the same checks without changing the cluster:

```bash
make check-scheduling-resilience
```

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
  --connect-ip 192.168.1.12 \
  --duration 300 \
  --cooldown 90 \
  --output artifacts/fd-stability.json
```

Export `OPENAI_API_KEY` as shown in the endpoint check above before running.
The regression fails on any `EMFILE` response, an excessive request error
rate, FD utilization above 80 percent, missing telemetry, or FD counts that do
not return close to the per-pod baseline after cooldown.

## Security

Do not commit Hugging Face tokens, rendered Secrets, or local `.env` files.
Keep NodePorts and any LoadBalancer address behind trusted networks. PVCs and
app secrets are preserved unless explicitly deleted.
