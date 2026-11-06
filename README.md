# Daedalus Dynamo

Portable single-node NVIDIA Dynamo deployment recipes for DGX Spark, L40S and A30.
The catalog preserves a 2026-10-30 snapshot of all 93 entries in NVIDIA's
inference-optimized checkpoint collection. The current tree supplies 116 catalog
recipes across 42 model definitions, including two supplemental Spark models and
one specialized NVMe-offload recipe. A separate production overlay records the
home-cluster Muse configuration. Candidate recipes require GPU qualification;
recorded model-specific checks are not general performance guarantees.

See the [recipe guide and complete model matrix](docs/model-recipes.md),
[chart contract](charts/dynamo-model/README.md), and
[recorded collection decisions](config/model-catalog/recipes.json).

## Repository layout

- `charts/`: model and shared Model Express runtime Helm charts
- `values/`: layered model recipes, hardware defaults, and operational overlays
- `runtime/`: custom runtime Dockerfiles and compatibility patches
- `scripts/`: recipe resolution, catalog generation, gateway and cluster tooling
- `config/`: catalog snapshots, source locks, and supporting cluster configuration
- `benchmarks/aiperf/`: pinned agentic workload and Docker/Kubernetes runners
- `observability/`: Prometheus configuration and Grafana dashboards
- `tests/`: local regression tests and frozen legacy fixtures

Registry names, LAN addresses, certificate references, and the production node
placement describe the Daedalus home cluster. Review these settings before using
the recipes on another cluster. Operational details are in the
[observability guide](observability/README.md) and
[NVIDIA Spark recipe guide](docs/nvidia-spark-recipes.md).

## Model organization

```text
values/
  _shared/vllm.yaml
  _hardware/{dgx-spark,l40s,a30}.yaml
  <builder>/<checkpoint>/model.yaml
  <builder>/<checkpoint>/_tuning.yaml
  <builder>/<checkpoint>/<hardware>/_tuning.yaml
  <builder>/<checkpoint>/<hardware>/_tuning-<parallelism>.yaml
  <builder>/<checkpoint>/<hardware>/tp1-pp1-dp1.yaml
  <builder>/<checkpoint>/l40s/tp2-pp1-dp1.yaml
  production/muse-glimmer.yaml
  maintenance/_muse-glimmer-gb10-bridge.yaml
```

Builder means the original family owner (NVIDIA, Qwen, Meta, Google, Microsoft,
Mistral); each model definition records the exact Hugging Face publisher ID.
A model definition inherits serving defaults. A recipe adds hardware, parallelism,
release identity and storage. Mappings merge; lists replace. `make` resolves the
layers before passing ordinary values to Helm.

Generic recipes inherit `worker.vllmOptions.max-model-len: auto`: vLLM determines
the largest context that fits its configured memory budget at startup, up to the
model's supported limit. NVIDIA Spark profiles override this with their published
context policy, including an omitted limit for Lightning. Numeric
`recipe.screeningContextLength` values are only for offline memory screening and
simulation; they do not cap serving context. Optional tuning layers preserve
model, hardware, and parallelism-specific settings when regenerating recipes.

## Select and deploy

The current home-cluster Muse deployment uses
[`values/production/muse-glimmer.yaml`](values/production/muse-glimmer.yaml).
It pins two L40S devices, the frontend and ModelExpress to node 03, uses a retained
local-fast cache, and enables decode CUDA graphs with the pinned DFlash assistant.
The established release/Service names still contain `tp1` for client compatibility;
the production worker actually requests two GPUs and runs tensor parallelism 2.
The generated TP1 recipe is an alternative configuration for the same release,
so use the production file when reconciling the active deployment.

The production profile preserves the model, vision/tool parsers and automatic
context sizing; acceptance requires a runtime context of 131072 tokens. Its GPUs
are fully occupied. For uninterrupted upgrades, validate the private GB10 bridge
in `values/maintenance/_muse-glimmer-gb10-bridge.yaml`, switch the existing route
to that frontend, and drain active requests before replacing the production
worker. Restore the original route after private capability checks, then verify
LAN APIs before removing the temporary release. Keep the bridge's retained PVCs.

```bash
make models
make lint values/qwen/qwen3-8b-nvfp4/l40s/tp1-pp1-dp1.yaml
make render values/qwen/qwen3-8b-nvfp4/l40s/tp1-pp1-dp1.yaml
make deploy values/qwen/qwen3-8b-nvfp4/l40s/tp1-pp1-dp1.yaml
```

`deploy` reconciles the shared Model Express startup ConfigMap in the namespace,
creates or updates the Hugging Face secret when `HF_TOKEN` is exported (otherwise
reuses the existing secret), and installs the selected model release.
Prebuilt runtime recipes skip local image builds. Recipes with
`recipe.buildRequired: true`, including the 12 NVIDIA Spark profiles, Muse L40S
TP1, and Qwen Flash Next NVMe, build and push their custom images by default.
Use `SKIP_BUILD=1 SKIP_PUSH=1` only when the selected image is already available
to the cluster; ARM64 builds need a compatible builder.

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

The previous flat/staging deployment profiles were retired.
Frozen historical settings remain only under `tests/fixtures/retired-values/`
for regression tests and are excluded from `make models`. The stack-upgrade
script no longer redeploys the retired models. Retirement backups and live
teardown evidence are local-only under `artifacts/portability-20261030/` and are
not included in a Git clone.

## Reproduce the catalog and simulations

```bash
python3 scripts/build_model_catalog.py
python3 scripts/simulate_model_catalog.py
python3 -m unittest discover -s tests -p test_recipe_portability.py
python3 -m unittest discover -s tests -p test_model_cache_runtime.py
```

The generator rewrites catalog outputs and generated recipes using the committed
collection snapshot, compressed hash-checked HF configurations, and NVIDIA Spark
source lock. Review its diff before committing. The simulation runner needs
Docker and uses a digest-pinned Planner 1.6.1 image for CPU-only offline prediction.
It records failures as well as estimates and declines specialized NVIDIA Spark
profiles that its generic adapter cannot model. See the recipe guide for missing
GB10/A30 coverage and estimator/runtime version differences.

## Prerequisites

The deployment host needs:

- Bash, GNU Make, Docker (with Compose for local benchmarks), Helm, `kubectl`,
  `curl`, and Python 3.11 or newer with PyYAML
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

For local recipe resolution and tests, install PyYAML in an isolated environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install PyYAML
```

## First-Time Installation

All commands below are run from the repository root. Confirm the intended
cluster before installing cluster-scoped APIs:

```bash
kubectl config current-context
make models
```

If `hf-token-secret` does not already exist in the deployment namespace, read
the token without writing it into shell history, then create the Secret:

```bash
read -rsp 'Hugging Face token: ' HF_TOKEN; echo
export HF_TOKEN
make secret
unset HF_TOKEN
```

`make secret` creates the namespace if needed. Make does not automatically load
the repository's ignored `.env` file.

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

### 4. Deploy and check a model

After choosing a recipe and checking GPU capacity, run the lint, render, and
deploy commands in [Select and deploy](#select-and-deploy). On the existing home
cluster, use the production Muse overlay when reconciling that release.

From a LAN client with Python installed, enter the existing gateway bearer key
and verify discovery and a Responses API request:

```bash
read -rsp 'Gateway API key: ' OPENAI_API_KEY; echo
export OPENAI_API_KEY
curl --fail --silent --show-error \
  -H "Authorization: Bearer ${OPENAI_API_KEY}" \
  http://192.168.1.12:32360/v1/models
python3 scripts/smoke-test-responses.py --connect-ip 192.168.1.12
```

The smoke test selects the first advertised model unless `--model <model-id>` is
provided. For the existing HTTPS path, use
`--url https://daedalus.ddns.me:32359 --connect-ip 192.168.1.12`; certificate
verification remains enabled. Include a non-node LAN client when validating
access. Keep `OPENAI_API_KEY` exported for subsequent checks, then unset it when
finished.

## Agentic Gateway Performance Evaluation

Run the pinned AIPerf 0.13.0 multi-turn workload against every model currently
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
  AIPERF_IMAGE=registry.example.com/daedalus/aiperf:0.13.0 \
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
and Grove to the configured minimum versions (currently `1.6.1` and
`v0.1.0-alpha.17` in the Makefile),
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

## Local validation and Git hygiene

Recipe linting and rendering run locally without deploying workloads:

```bash
make lint values/qwen/qwen3-8b-nvfp4/l40s/tp1-pp1-dp1.yaml
make render values/production/muse-glimmer.yaml > /tmp/muse-rendered.yaml
python3 -m unittest discover -s tests -p test_recipe_portability.py
python3 -m unittest discover -s tests -p test_nvidia_spark_recipes.py
python3 -m unittest discover -s tests -p test_model_cache_runtime.py
```

To run the entire regression suite, use `python3 -m unittest discover -s tests`.
Helm is required for chart tests. These checks do not establish GPU readiness or
model performance. The [Spark implementation notes](docs/nvidia-spark-recipes.md)
record known full-suite failures from the earlier validation baseline.

Before committing, inspect the files Git would publish and run the existing
credential guard over tracked and nonignored new files:

```bash
git status --short
git diff --check
git ls-files -z --cached --others --exclude-standard | \
  xargs -0 bash scripts/pre-commit/check-secrets.sh
git diff --cached --stat
```

`.gitignore` excludes local credentials, virtual environments, Python caches,
build output, rendered manifests, model weights, logs, and experiment evidence
under `artifacts/`, `runs/`, and `tmp/`. Catalog JSON, compressed HF configuration
snapshots, benchmark lockfiles, runtime patches, and production/maintenance
recipes belong in Git. Ignore rules do not remove files already tracked or
erase prior commits; the credential guard is a heuristic check of working files,
not a complete audit of Git history.

## Security

Do not commit Hugging Face tokens, rendered Secrets, or local `.env` files.
Keep NodePorts and any LoadBalancer address behind trusted networks. PVCs and
app secrets are preserved unless explicitly deleted.
