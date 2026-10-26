# Daedalus Dynamo Helm Deployments

This repository deploys NVIDIA Dynamo model backends with one reusable Helm
chart and model-specific values files. The root [`Makefile`](./Makefile) is the
primary entrypoint for building images, rendering charts, deploying releases,
and uninstalling releases from model config paths.

| Model config | Model | Runtime |
| --- | --- | --- |
| `values/glm47flash.yaml` | `unsloth/GLM-4.7-Flash-NVFP4` | vLLM |
| `values/nemotron-omni.yaml` | `nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-NVFP4` | vLLM |
| `values/qwen35b.yaml` | `nvidia/Qwen3.6-35B-A3B-NVFP4` | vLLM |
| `values/qwen27b.yaml` | `Qwen/Qwen3.8-27B-FP8` | vLLM |
| `values/qwen38-27b-l40s.yaml` | `Qwen/Qwen3.8-27B-FP8` | vLLM, fixed text-only TP1 on L40S |
| `values/gemma431b.yaml` | `nvidia/Gemma-4-31B-IT-NVFP4` | vLLM |
| `values/nemotronnano.yaml` | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-Reasoning-NVFP4` | vLLM |
| `values/nemotron35lightning-gb10.yaml` | `nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4` | TensorRT-LLM |
| `values/qwen38-flash-next-gb10.yaml` | `nvidia/Qwen3.8-Flash-Next-NVFP4` | vLLM, GB10 with NVMe-backed embeddings |
| `values/nemotron35bolt-gb10.yaml` | `nvidia/NVIDIA-Nemotron-3.5-Bolt-74B-A9B-24GB` | vLLM, GB10, prefix caching |

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
export GATEWAY_IMAGE=registry.example.com/daedalus/agentgateway:v1.4.1
export IMAGE=registry.example.com/daedalus/dynamo-vllm-runtime:1.4.1
docker login registry.example.com
```

Both references must include a registry, repository, and tag. For a private
registry, configure node-level pull credentials before deployment; the chart
does not create registry credentials.

### 2. Install the Inference Gateway

The gateway uses the existing `daedalus/daedalus-tls` certificate Secret and
`daedalus-dynamo/inference-gateway-api-keys` authentication Secret. Provision
these before installation. The checked-in certificate grant permits the
`daedalus-dynamo` gateway to reference that certificate. Clients use verified
HTTPS with a bearer key on port `32359`.

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
assume that every model is deployed together. The Makefile deploy flow also
applies the shared `inference-gateway`, model-header policy, `/v1/models`
catalog, and the selected model's HTTPRoute.

## Deploy A Model

Every model action takes one relative or absolute `.yaml` or `.yml` config
path. Paths containing whitespace are unsupported because GNU Make receives
the config as a goal. There is no default model, model-name alias, or batch
target; invoke each intended config explicitly:

```bash
HF_TOKEN=<hugging-face-token> make deploy values/qwen35b.yaml
```

The config filename stem is the default Helm release and model-route suffix.
For a renamed or copied config that must retain an existing identity, pass
`RELEASE=<release>` and, when needed, `MODEL_ROUTE_NAME=<route>` explicitly.

Deploy another model by passing its config path:

```bash
HF_TOKEN=<hugging-face-token> make deploy values/glm47flash.yaml
HF_TOKEN=<hugging-face-token> make deploy values/qwen27b.yaml
HF_TOKEN=<hugging-face-token> make deploy values/gemma431b.yaml
HF_TOKEN=<hugging-face-token> make deploy values/nemotron-omni.yaml
HF_TOKEN=<hugging-face-token> make deploy values/nemotronnano.yaml
SKIP_BUILD=1 SKIP_PUSH=1 HF_TOKEN=<hugging-face-token> \
  make deploy values/nemotron35lightning-gb10.yaml
```

`make deploy` builds the image, pushes it, creates or reuses
`hf-token-secret`, applies the shared inference gateway resources, runs
`helm upgrade --install`, then refreshes the selected model route and model
catalog. To deploy an already pushed image:

```bash
SKIP_BUILD=1 SKIP_PUSH=1 HF_TOKEN=<hugging-face-token> \
  make deploy values/qwen35b.yaml
```

The explicit `make install-gateway` step must have succeeded at least once.
Normal model deployments reconcile gateway resources but do not reinstall its
cluster-scoped CRDs or control plane.

For an existing release whose resources were created outside Helm, use Helm
3.17+ ownership adoption:

```bash
TAKE_OWNERSHIP=1 SKIP_BUILD=1 SKIP_PUSH=1 HF_TOKEN=<hugging-face-token> \
  make deploy values/qwen35b.yaml
```

Check the release and endpoint after deployment:

```bash
make status values/qwen35b.yaml
kubectl get dgd,pods,httproute -n daedalus-dynamo
kubectl get svc inference-gateway -n daedalus-dynamo
export OPENAI_API_KEY="$(kubectl get secret inference-gateway-api-keys \
  -n daedalus-dynamo -o jsonpath='{.data.switchyard}' | base64 --decode)"
curl --fail --resolve daedalus.ddns.me:32359:192.168.1.12 \
  -H "Authorization: Bearer ${OPENAI_API_KEY}" \
  https://daedalus.ddns.me:32359/v1/models
```

The gateway requires HTTPS and a bearer key. The hostname remains
`daedalus.ddns.me` for certificate verification; `--resolve` selects the LAN
VIP without changing DNS. An existing `OPENAI_API_KEY` can be used instead of
loading the Secret. For local access without a reachable LoadBalancer or
NodePort, run the forward in one terminal and the authenticated request in another:

```bash
kubectl port-forward -n daedalus-dynamo svc/inference-gateway 8000:32359
curl --fail --resolve daedalus.ddns.me:8000:127.0.0.1 \
  -H "Authorization: Bearer ${OPENAI_API_KEY}" \
  https://daedalus.ddns.me:8000/v1/models
```

Run one Responses API request against the first model in that catalog:

```bash
scripts/smoke-test-responses.py --connect-ip 192.168.1.12 --max-output-tokens 512
```

Pass `--model <model-id>` to select a specific deployment. The script also
accepts `ENDPOINT_URL`, `ENDPOINT_CONNECT_IP`, `MODEL_ID`, and `OPENAI_API_KEY`
from the environment. Both client scripts default to verified HTTPS and the
LAN VIP for `daedalus.ddns.me`; `--connect-ip ''` uses normal DNS instead.
Unset `OPENAI_API_KEY` when finished.
Reasoning models may need a larger output budget than the script's default
128 tokens to reach a completed response.

## Nemotron 3.5 Bolt on Spark

[`values/nemotron35bolt-gb10.yaml`](./values/nemotron35bolt-gb10.yaml)
serves the exact NVIDIA 24GB IQ2 checkpoint on
one GB10. It configures 131,072 tokens, tensor parallel size 1, a 64-sequence
admission ceiling, 48 GiB KV cache, and a 16-token prefix-match unit. DFlash is
disabled because its sliding-window cache manager prevents fine-grained reuse
in this runtime. The model revision is pinned. The model cache and compiled kernels use Spark's local
NVMe.

INC-01 tuning uses an 8,192-token batch budget and limits each long prefill to
1,024 tokens under contention. CUDA graphs are disabled while compiled kernels
remain enabled: larger-concurrency graph configurations failed on their first
request in this SM121 preview runtime. The sequence ceiling is not a guarantee
of 64 simultaneous full-context requests; KV demand and compute still bound
concurrency. See the [runtime notes](./runtime/nemotron35bolt-gb10/README.md)
for cache configuration and validation results.

This checkpoint's b12x IQ2 kernels require Blackwell SM120/SM121; the cluster's
L40S GPUs cannot run the supported packed-weight backend. The profile therefore
requires stopping the Qwen3.8 Flash Next release that otherwise occupies the
GB10. Uninstall retains both releases' model-cache PVCs for reuse.

The custom ARM64 runtime pins NVIDIA's model-card vLLM revision
`77154ca6116ae34cbfe314beef1016bde9b63989`, Dynamo
`1.6.0.dev20261019`, and b12x `1.5.1`. This is a preview runtime; the stable
shared image predates the required Bolt and LiLiCorr support.

Validated on the cluster on 2026-10-20: text, image input, concurrent requests,
and streaming text plus automatic and forced tool round trips through both
Chat Completions and Responses. A 130,992-token prompt passed through Dynamo
and the authenticated gateway; a 128,392-token prompt passed through Switchyard's
`daedalus/local` alias. Repeated long prompts reused 107,904 cached tokens.
Worker metrics confirmed active speculative decoding. Muse and Qwen are stopped,
and Switchyard's four Muse-containing route profiles now select Bolt.
Validation records are in `artifacts/nemotron35bolt-migration/`.

```bash
DOCKER_HOST=ssh://daedalus-06 make image values/nemotron35bolt-gb10.yaml
SKIP_BUILD=1 SKIP_PUSH=1 make deploy values/nemotron35bolt-gb10.yaml
make status values/nemotron35bolt-gb10.yaml
```

Sources: [NVIDIA model card](https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Bolt-74B-A9B-24GB),
[LiLiCorr model card](https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Bolt-74B-A9B-24GB-LiLiCorr),
and [vLLM b12x hardware support](https://github.com/vllm-project/vllm/blob/77154ca6116ae34cbfe314beef1016bde9b63989/docs/features/quantization/b12x.md).

## Qwen3.8 27B FP8 on one L40S

[`values/qwen38-27b-l40s.yaml`](./values/qwen38-27b-l40s.yaml) is a separate
fixed-capacity release named `qwen38-27b-vllm-l40s`. It preserves the existing
TP2 `qwen27b.yaml` recipe and existing model endpoints. The worker and frontend
pin the Dynamo 1.4.1 image by digest; the worker selects model revision
`017b9c7af6b5689d5dd426a76e0bc077eb5ca20a` with `--revision` and serves the
model ID `Qwen/Qwen3.8-27B-FP8`.

This fixed single-node release sets the direct DGD metadata annotation
`nvidia.com/enable-grove: "false"` through `model.annotations`. The installed
Dynamo operator uses ordinary component Deployments instead of Grove gang
recovery for this model. Other profiles retain their existing scheduling path.
The frontend and ModelExpress Service names remain
`qwen38-27b-vllm-l40s-frontend` and `qwen38-27b-vllm-l40s-modelexpress`.

Select the scheduling backend when creating the DGD. Converting an existing
Grove DGD requires a reviewed, scoped recreation: its stored `minAvailable`
defaults are incompatible with the non-Grove path, a metadata-only toggle does
not trigger the generation watcher, and old Grove resources are not migrated
automatically. Retain the separate model-cache PVC, Redis, HTTPRoute and serving
configuration, remove only this model's old DGD and dependent compute, and
recreate it from the reviewed values. Account for the brief frontend outage;
keep application routing on the existing backend until the replacement passes
readiness and endpoint checks. Do not change the global operator or other DGD
profiles to perform this migration.

The profile uses one L40S, TP1, text-only frontend decoding, a 131,072-token
model limit, four sequence slots, a 4,096-token batch budget, and an explicit
8 GiB FP8 KV pool. Prefix caching and chunked prefill are enabled; speculative
decoding, Planner, and the scaling adapter are disabled. Four slots do not
promise four concurrent full-context requests. Verify the runtime's reported
KV token capacity and an actual full-context request before accepting the
configured context limit as usable capacity.

The frontend selects the vLLM Python chat processor with automatic tool
selection and the `qwen3_coder` tool and `qwen3` reasoning parsers. The worker
retains the native parser metadata and `--dyn-enable-structural-tag`, and sets
the engine's `--reasoning-parser qwen3`. The engine reasoning parser allows
thinking before constrained tool arguments; structural tags preserve the Qwen
tool-call framing consumed by the frontend postprocessor. The Python frontend
leaves `tool_choice=none` without a tool grammar. Structural-tag scope and schema
selection retain their runtime defaults. Qualify automatic, named, required,
and tool-free continuation responses through this exact combined path before
using the endpoint for the application workflow.

The Python frontend requests 4 GiB host memory with an 8 GiB limit; its
inherited CPU request and limit remain 500m and 2 cores. The generic Rust
frontend's 2 GiB memory limit is insufficient for this Python processing path.

The worker requests 48 GiB host memory. Its frontend, ModelExpress, and worker
all select `daedalus-03` so the distinct 64 GiB `local-fast` RWO model-cache
claim binds beside every consumer. Redis uses a separate 1 GiB local claim.
ModelExpress prepopulates the upstream default revision. The worker uses the
standard safetensors loader and revision-aware Hugging Face resolver for its
configuration and weights; a missing pinned snapshot can require a separate
download. Confirm it actually loads the pinned `017b9c7...` snapshot before
qualifying the endpoint. Do not move a cache consumer to another node
independently.

For an additional backend without reconciling the shared gateway, use the
existing deployment controls after reviewing the rendered manifests:

```bash
make lint values/qwen38-27b-l40s.yaml
make render values/qwen38-27b-l40s.yaml
GATEWAY=0 SKIP_BUILD=1 SKIP_PUSH=1 SKIP_SECRET=1 make deploy values/qwen38-27b-l40s.yaml
```

This profile defines launch settings, not a quality or latency result. Keep
existing serving routes until startup, model identity, capacity, tool parsing,
and the intended application workflow pass their endpoint checks.

## Qwen3.8 Flash Next on Spark

[`values/qwen38-flash-next-gb10.yaml`](./values/qwen38-flash-next-gb10.yaml)
uses NVIDIA's official NVFP4 checkpoint. The requested FP8 checkpoint exceeds
Spark's unified memory. Even the NVFP4 checkpoint needs its n-gram embedding
table mapped from disk, so this profile uses a custom ARM64 runtime and a
Spark-local `local-fast` model cache. It starts one text-only worker with
native 262K context, four sequences, 8 GiB KV cache, and MTP disabled.

Build and push on the Spark's native Docker daemon, then deploy using the
existing secret and gateway:

```bash
DOCKER_HOST=ssh://daedalus-06 make image values/qwen38-flash-next-gb10.yaml
SKIP_BUILD=1 SKIP_PUSH=1 make deploy values/qwen38-flash-next-gb10.yaml
make status values/qwen38-flash-next-gb10.yaml
```

See the [runtime notes](./runtime/qwen38-flash-next-gb10/README.md) for source
pins, memory sizing, patch provenance and validation. The official checkpoint
weights are unchanged; the runtime supplies the disk-backed lookup path.
The release was validated live on Spark: chat, tool calls, streaming,
Responses, three concurrent prefix-cache requests, and a 250,001-token
retrieval passed. The shared gateway advertises
`nvidia/Qwen3.8-Flash-Next-NVFP4`; this initial profile serves text with MTP off.

## Nemotron 3.5 Lightning GB10 Release

The `values/nemotron35lightning-gb10.yaml` config adds initial single-node
TensorRT-LLM support for
[`nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4`](https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4).
It adapts NVIDIA's experimental
[one-GPU B200 MTP recipe](https://github.com/ai-dynamo/dynamo/blob/main/recipes/nemotron-3.5-lightning/trtllm/agg-b200-mtp/deploy.yaml)
to a node labeled `nvidia.com/gpu.product=NVIDIA-GB10`.

| Setting | Release value |
| --- | --- |
| Runtime | `nvcr.io/nvidia/ai-dynamo/tensorrtllm-runtime:1.5.0-nemotron-3.5-lightning-dev.1` |
| Backend and topology | TensorRT-LLM aggregate, tensor parallel size 1, one worker, one GPU |
| Speculative decoding | MTP with three draft tokens |
| Initial batch capacity | `max_batch_size: 8`, reduced from the upstream value of 512 |
| Initial token capacity | `max_num_tokens: 32768`, unchanged from upstream |
| Worker memory | 96 GiB request and 16 GiB `/dev/shm` |
| Model cache | 96 GiB RWX PVC, populated by ModelExpress |
| Autoscaling | Disabled; the release has one worker and one available GPU |
| Hub access | Public and non-gated; the chart still expects `hf-token-secret` |
| License | [OpenMDW 1.1](https://openmdw.ai/license/1-1/) |

The runtime and ModelExpress images publish `linux/arm64` variants. The target
node must expose one allocatable `nvidia.com/gpu` resource and the exact GB10
product label. The deployment also needs an RWX-capable default StorageClass
and `hf-token-secret` with read access to the model.

Check those release prerequisites before deployment:

```bash
kubectl get nodes -l 'nvidia.com/gpu.product=NVIDIA-GB10' \
  -o custom-columns='NAME:.metadata.name,ARCH:.status.nodeInfo.architecture,MEMORY:.status.allocatable.memory,GPUS:.status.allocatable.nvidia\.com/gpu'
kubectl get storageclass
kubectl get secret hf-token-secret -n daedalus-dynamo
make lint values/nemotron35lightning-gb10.yaml
make render values/nemotron35lightning-gb10.yaml \
  >/tmp/nemotron35lightning-gb10.yaml
kubectl apply --dry-run=server -n daedalus-dynamo \
  -f /tmp/nemotron35lightning-gb10.yaml
```

The release uses NVIDIA's published runtime directly. Skip the repository's
vLLM image build and push steps:

```bash
SKIP_BUILD=1 SKIP_PUSH=1 \
  make deploy values/nemotron35lightning-gb10.yaml
```

If `hf-token-secret` does not exist, provide `HF_TOKEN` on that invocation so
the Makefile creates it without writing the token to a file. After deployment,
verify the release, cache downloader, worker placement, model catalog, and one
response:

```bash
make status values/nemotron35lightning-gb10.yaml
kubectl get dgd,pods,pvc,httproute -n daedalus-dynamo -o wide
kubectl logs -n daedalus-dynamo \
  -l 'nvidia.com/dynamo-component=ModelExpress,nvidia.com/dynamo-graph-deployment-name=nemotron-35-lightning-trtllm-gb10' \
  --tail=100
curl --fail --resolve daedalus.ddns.me:32359:192.168.1.12 \
  -H "Authorization: Bearer ${OPENAI_API_KEY}" \
  https://daedalus.ddns.me:32359/v1/models
curl --fail --resolve daedalus.ddns.me:32359:192.168.1.12 \
  -H "Authorization: Bearer ${OPENAI_API_KEY}" \
  https://daedalus.ddns.me:32359/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4",
    "messages": [{"role": "user", "content": "Say OK."}],
    "max_tokens": 128,
    "temperature": 0
  }'
```

Treat batch size as the first memory-control lever. If the engine cannot
initialize, try 4, 2, and then 1. Reduce `max_num_tokens` only after batch 1
still fails. For example:

```bash
HELM_ARGS='--set worker.trtllm.engineConfig.max_batch_size=4' \
  make upgrade values/nemotron35lightning-gb10.yaml

HELM_ARGS='--set worker.trtllm.engineConfig.max_batch_size=1 --set worker.trtllm.engineConfig.max_num_tokens=16384' \
  make upgrade values/nemotron35lightning-gb10.yaml
```

The chart writes these capacity values to the TensorRT-LLM engine file and CLI
from one source, so the two interfaces remain aligned. ModelExpress downloads
the checkpoint into the shared PVC, resets only stale registry metadata after
an interrupted download, and creates the cache-ready link before the worker
starts.

This is an initial compatibility release. The runtime images, Helm render,
repository tests, server-side Kubernetes dry-run, live GB10 scheduling, and
ModelExpress `ServerOnly` download path have been validated. Treat engine and
endpoint qualification as pending unless the current release reaches Ready and
passes the model-specific smoke request above. NVIDIA's current
[TensorRT-LLM recipe inventory](https://github.com/ai-dynamo/dynamo/blob/main/recipes/nemotron-3.5-lightning/trtllm/README.md)
lists H100, H200, B200, and GB200, but not GB10. Use the observed initialization
result to choose the next batch or token reduction; do not describe this profile
as performance-qualified yet.

## Common Commands

```bash
make models
make install-gateway
make lint values/qwen35b.yaml
make render values/qwen35b.yaml
make dry-run values/qwen35b.yaml
make gateway values/qwen35b.yaml
make upgrade values/qwen35b.yaml
make status values/qwen35b.yaml
make uninstall values/qwen35b.yaml
```

`make upgrade` runs only Helm. `make deploy` is the full image plus Helm flow.
Use `make upgrade <model-config.yaml>` for chart-only changes that leave the
model identity, route, secret, and gateway unchanged. When `model.name`,
`model.id`, the namespace, or gateway settings change, use the full deploy
path with `SKIP_BUILD=1 SKIP_PUSH=1`; it also refreshes the HTTPRoute and
`/v1/models` catalog.

`make uninstall` removes the Helm release, deletes that model's HTTPRoute, and
refreshes the catalog. Chart-managed PVCs are annotated with
`helm.sh/resource-policy: keep`, so model cache and Redis data claims remain
after Helm uninstall.

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
`https://daedalus.ddns.me:32359/v1/models`, using a pod-local hostname mapping
directly to the gateway Service's ClusterIP and `OPENAI_API_KEY` from Secret
`inference-gateway-api-keys`, key `switchyard`. The model list is not derived
from checked-in values files. The hostname supplies the existing certificate's
TLS identity; inference traffic stays inside the cluster and does not use public
DNS or the LAN VIP. `AIPERF_CONNECT_IP=auto` resolves the current ClusterIP on
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

## Images

By default, the Makefile builds the root [`Dockerfile`](./Dockerfile), which
uses NVIDIA's nightly Dynamo vLLM runtime image as its base. It reads
`model.image` from the selected values file and uses that repository and tag
for both Docker and Helm. The vLLM model values share that image so one build
and node pull can be reused. Backend-specific specifications can instead use a
published runtime directly; deploy those with `SKIP_BUILD=1 SKIP_PUSH=1`.

Build and push the shared runtime once, then deploy without rebuilding:

```bash
make build values/glm47flash.yaml
make push values/glm47flash.yaml
SKIP_BUILD=1 SKIP_PUSH=1 HF_TOKEN=<hugging-face-token> \
  make deploy values/glm47flash.yaml
```

Override the resulting image tag or the Dockerfile's nightly base image when
needed:

```bash
IMAGE_TAG=test make build values/qwen35b.yaml
IMAGE=registry.example.com/dynamo/qwen35b:test \
  make deploy values/qwen35b.yaml
BASE_IMAGE=nvcr.io/nvidia/ai-dynamo/vllm-runtime-nightly:latest \
  make build values/qwen35b.yaml
```

## Helm Values

Model-specific settings live in [`values/`](./values). Shared defaults live in
[`charts/dynamo-model/values.yaml`](./charts/dynamo-model/values.yaml).

The model files are independent deployment alternatives, not a bundle to run
simultaneously. The vLLM alternatives target the L40S pool and cap Planner at
the two GPUs currently left available by the retriever workloads. The GB10
alternative is pinned to its single product-labeled GPU and does not autoscale.

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
make render values/qwen35b.yaml
```

Render the DGDR source object instead:

```bash
DEPLOYMENT_MODE=dgdr make render values/qwen35b.yaml
```

Pass arbitrary Helm overrides through `HELM_ARGS`:

```bash
HELM_ARGS='--set planner.advisory=true --set planner.maxGpuBudget=2' \
  SKIP_BUILD=1 SKIP_PUSH=1 HF_TOKEN=<hugging-face-token> \
  make deploy values/qwen35b.yaml
```

Validate configuration before applying it:

```bash
make lint values/qwen35b.yaml
make dry-run values/qwen35b.yaml
```

To add a model, copy an existing file under `values/`, change at least
`model.name`, `model.id`, `model.image`, and the worker arguments, then run
`make lint values/<new-file>.yaml`. The `models` target discovers checked-in
values files automatically; no per-model deployment target or script is
needed.

The `nemotron35lightning-gb10` specification uses the official
multi-architecture TensorRT-LLM recipe image. See
[Nemotron 3.5 Lightning GB10 Release](#nemotron-35-lightning-gb10-release) for
its prerequisites, initial memory profile, deployment procedure, tuning order,
and qualification status.

Useful Makefile variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `RELEASE` | model config filename stem | Helm release name. |
| `MODEL_ROUTE_NAME` | `daedalus-dynamo-<config filename stem>` | HTTPRoute resource name. |
| `NAMESPACE` | `daedalus-dynamo` | Kubernetes namespace. |
| `IMAGE` | selected `model.image` | Full runtime image reference. |
| `IMAGE_TAG` | tag from selected `model.image` | Runtime image tag override. |
| `BASE_IMAGE` | Dockerfile's pinned Dynamo nightly image | Docker build base image. |
| `DEPLOYMENT_MODE` | values file setting | Optional `direct` or `dgdr` override. |
| `TAKE_OWNERSHIP` | `0` | Pass Helm `--take-ownership`. |
| `SKIP_BUILD` / `SKIP_PUSH` | `0` | Skip image steps during `make deploy`. |
| `SKIP_SECRET` | `0` | Skip HF token secret check/update during `make deploy`. |
| `GATEWAY` | `1` | Apply shared inference gateway resources, model route, and model catalog during `make deploy`. |
| `INSTALL_GATEWAY` | `0` | Install or upgrade Gateway API, GAIE, and agentgateway before applying gateway resources. |
| `GATEWAY_IMAGE` | internal `daedalus-agentgateway:v1.4.1` | Hardened agentgateway image built by `make gateway-image`. |
| `GATEWAY_REPLICAS` | `2` | Static proxy replica floor, aligned with the HPA minimum. |
| `GATEWAY_NOFILE_LIMIT` | `131072` | Verified proxy `RLIMIT_NOFILE` soft limit. |
| `GATEWAY_HPA_MIN_REPLICAS` / `GATEWAY_HPA_MAX_REPLICAS` | `2` / `8` | Proxy HPA range. |
| `GATEWAY_POOL_MAX_SIZE` | `64` | Maximum idle upstream connections retained per host. |
| `SKIP_GATEWAY_BUILD` / `SKIP_GATEWAY_PUSH` | `0` | Reuse an already published hardened gateway image. |
| `HF_TOKEN_SECRET` | `hf-token-secret` | Secret referenced by Dynamo services. |
| `HELM_ARGS` | unset | Extra args for lint, render, dry-run, and upgrade. |

`planner.workerNamespaceCompat=off` is the chart default for the installed
Planner 1.4.0 stack, which performs normal Grove worker namespace discovery.
Use `legacy` only with an older stack that requires the chart's base-namespace
compatibility wrapper. Set `hashed` only when the worker runtime actually
publishes metadata under the DGD worker-hash namespace.

## Developer Notes

Keep deployment behavior centralized in the root Makefile and model behavior in
the Helm chart plus `values/*.yaml`. Avoid adding model-specific deploy scripts
or Kubernetes YAML branches.

Recommended checks:

```bash
make lint values/qwen35b.yaml
make validate-resilience
make render values/qwen35b.yaml | kubectl apply --dry-run=client -f -
```

## Security

Do not commit Hugging Face tokens, rendered Secrets, or local `.env` files.
Keep NodePorts and any LoadBalancer address behind trusted networks. PVCs and
app secrets are preserved unless explicitly deleted.
