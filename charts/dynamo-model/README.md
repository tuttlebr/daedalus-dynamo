# Dynamo Model Helm Chart

This chart renders one Dynamo model backend release from a model values file.
It is intended to replace the repeated per-model Kubernetes YAML once the Helm
path is proven in the cluster.

Each file under `values/` is an alternative deployment profile. The profiles
are sized and validated individually; they are not intended to be installed
all at once.

Every profile starts with one model worker and one CPU frontend. Profiles with
autoscaling enabled retain their configured GPU budget and one-worker floor.

## Local Cluster Sizing

The vLLM profiles select GPUs with more than 40959 MiB, which admits the four
L40S GPUs and excludes the 24 GiB A30. Planner is capped at two workers because
the retriever currently occupies one L40S on each GPU node, leaving two L40S
GPUs available when a model alternative is selected. The shared 8 GiB worker
memory request, 2-core CPU request, and 16 GiB `/dev/shm` fit the 64 GiB L40S
node without making the second available GPU unschedulable. Worker memory has
no limit because model load has exceeded the schedulable request while actual
node memory still had headroom.

The GB10 profile uses an explicit product selector because that node does not
advertise `nvidia.com/gpu.memory`. It remains a one-worker profile with Planner
disabled and a 96 GiB host-memory request.

These settings are cluster-capacity and launch-safety defaults, not throughput
claims. Keep model-specific batch, context, and routing knobs unchanged until a
representative AIPerf workload provides TTFT, ITL, throughput, and error-rate
evidence for a one-variable-at-a-time change.

## Three-Actor SLA Alignment

The current workload has at most three active turns: one each from two people
and the autonomous bot. The chart maps that contract to deployment controls as
follows:

| SLA concern | Recipe control |
| --- | --- |
| Three active turns | DGDR concurrency is `3`; every vLLM `max-num-seqs` and TRT-LLM `max_batch_size` is at least `3` |
| Disruption controls | Frontend, worker, and Redis PodDisruptionBudgets; the initial single replicas provide no replica redundancy |
| First progress within 15 seconds | One warm worker through `planner.minEndpoint=1`; this still requires endpoint-level qualification |
| Bounded local GPU use | Planner may use at most the two L40S GPUs left available beside the retriever |
| Fast restart | Versioned image tags use `IfNotPresent`, while model and Redis PVCs remain reusable |

The PodDisruptionBudgets cover voluntary disruption only. They do not make the
single warm worker or a single cluster highly available, and they do not prove
the 10-minute recovery objective. Likewise, Planner `ttftMs` and `itlMs` are
backend scheduling inputs, not the chatbot's end-to-end first-progress or turn
completion timers. Qualify those objectives through the real Daedalus route.

The checked-in DGDR ISL/OSL pair is a planning proxy because the SLA does not
specify token distributions. Do not treat a DGDR plan or AIPerf result as
representative until a Daedalus trace establishes prompt, output, and tool-call
shape. The benchmark's default concurrency is SLA-aligned at three; its token
and session shape remain explicitly proxy-scoped.

## Render

```bash
helm template nemotronnano ./charts/dynamo-model \
  -n daedalus-dynamo \
  -f values/nemotronnano.yaml
```

Render the DGDR path instead of the direct DGD path:

```bash
helm template qwen35b ./charts/dynamo-model \
  -n daedalus-dynamo \
  -f values/qwen35b.yaml \
  --set model.deploymentMode=dgdr
```

## Install Or Upgrade

For a new backend:

```bash
helm upgrade --install nemotronnano ./charts/dynamo-model \
  -n daedalus-dynamo \
  -f values/nemotronnano.yaml
```

For a backend that was already created outside Helm, Helm 3.17+ can adopt the
existing resources:

```bash
helm upgrade --install nemotronnano ./charts/dynamo-model \
  -n daedalus-dynamo \
  -f values/nemotronnano.yaml \
  --take-ownership
```

The chart preserves the existing Redis selector contract
(`app.kubernetes.io/name`) so existing Redis Deployments can be adopted without
hitting immutable selector changes.

## Muse Glimmer on L40S

`values/muse-glimmer-l40s.yaml` starts one single-GPU worker, with TP1,
131,072-token context, BF16 KV cache, and bounded prefill and vision batches.
An additional replica can use a second L40S independently.
It uses a published Dynamo preview containing vLLM 0.28.0 for Muse support;
the shared stable image's vLLM 0.26.0 is too old. ModelOpt selects Marlin for
the checkpoint's W4A16 NVFP4 layers on L40S while preserving FP8/BF16 layers.

The profile uses `frontend.extraArgs` to select the vLLM chat processor and
Muse's reasoning/tool parsers. Each array entry is one literal CLI argument;
these options apply to direct DGD deployments. The fallback handles
`/v1/chat/completions`; it does not qualify Muse parsing through the Responses
or Anthropic APIs. The CPU frontend reserves additional memory
for Python/vLLM preprocessing.

```bash
make lint values/muse-glimmer-l40s.yaml
make render values/muse-glimmer-l40s.yaml
```

This is a hardware-informed baseline, not a measured performance result.
Qualify model startup, streamed reasoning/tool calls, image inputs, and
TTFT/ITL under the intended workload before deployment acceptance. The profile
uses the cluster's `nfs-client` model cache, `local-fast` Redis storage, and
usual `hf-token-secret`.

## TensorRT-LLM GB10 Example

Render the Nemotron 3.5 Lightning single-GB10 release:

```bash
helm template nemotron35lightning-gb10 ./charts/dynamo-model \
  -n daedalus-dynamo \
  -f values/nemotron35lightning-gb10.yaml
```

Install or upgrade it only after the model-cache StorageClass, Hugging Face
secret, GPU product label, and ARM64 runtime-image access have been checked:

```bash
helm upgrade --install nemotron35lightning-gb10 ./charts/dynamo-model \
  -n daedalus-dynamo \
  -f values/nemotron35lightning-gb10.yaml
```

This values file renders `backendFramework: trtllm`, one `TrtllmWorker`, and a
direct `nvidia.com/gpu.product: NVIDIA-GB10` selector. It disables Planner and
the worker scaling adapter because the release targets one GPU. ModelExpress
remains enabled and populates the 96 GiB shared cache before worker startup.

The initial engine profile keeps the upstream `max_num_tokens: 32768` while
reducing `max_batch_size` from 512 to 8. Continue reducing batch size through
4, 2, and 1 before reducing `max_num_tokens`. The chart reads both CLI capacity
flags from `worker.trtllm.engineConfig`, so a Helm override updates the engine
file and command line together.

This GB10 profile is validated by Helm lint/render, Kubernetes server-side
dry-run, live placement on the GB10 node, and the ModelExpress download path.
Treat engine and endpoint qualification as pending until the DGD reaches Ready
and a model-specific request succeeds. NVIDIA's current Nemotron 3.5 Lightning
TensorRT-LLM inventory does not include a GB10 recipe, so do not promote this
profile as performance-qualified without live workload evidence.

## Planner Namespace Compatibility

Planner 1.4.0 performs normal Grove worker namespace discovery, so
`planner.workerNamespaceCompat=off` is the default for this cluster.

When `planner.workerNamespaceCompat=legacy`, the chart starts Planner through a
small Python wrapper that keeps worker runtime discovery on the base
`DYN_NAMESPACE`. This matches operator/runtime stacks where workers publish
forward-pass metrics at:

```text
<k8s-namespace>-<dgd-name>.backend.generate
```

Set `planner.workerNamespaceCompat=hashed` only with an operator/runtime stack
that publishes worker metadata and forward-pass metrics under
`<DYN_NAMESPACE>-<worker-hash>`.

## Planner Warm Floors

The worker scaling adapter owns live worker replicas after a
`DynamoGraphDeployment` has been created. Do not change
`worker.replicas` on an existing deployment with the adapter enabled; the
admission webhook rejects it. Upgrade the Planner policy first, then establish
or raise a warm floor through the adapter:

```bash
kubectl scale dgdsa/<dgd-name>-<worker-component-lowercase> \
  -n <namespace> --replicas=<planner.minEndpoint>
```

Use the same floor in `planner.minEndpoint` so Planner does not immediately
scale it down. Let Planner remain the only autoscaler for that worker adapter;
use a distinct HPA only if the frontend itself is proven to be the bottleneck.

The old post-install annotation hook remains in values for emergency manual use,
but it is disabled by default and is not part of the normal deploy path.

## Current Scope

This chart owns model-local resources:

- model cache PVC
- Redis PVC, Service, and Deployment
- direct `DynamoGraphDeployment` or `DynamoGraphDeploymentRequest`
- Planner namespace compatibility settings
- voluntary-disruption protection for Redis, frontend, and the selected worker
- optional decode-worker `PodMonitor`

The root Makefile handles image build/push, cluster doctor, gateway installer,
and HF token secret creation around this chart.

The direct DGD path starts both `Frontend` and the configured model worker
through a shell guard that raises and verifies the `RLIMIT_NOFILE` soft limit.
The shared default is `131072`; override `frontend.nofileLimit` or
`worker.nofileLimit` only when the container runtime hard limit and expected
concurrency justify a different value.

Worker placement defaults to nodes whose `nvidia.com/gpu.memory` label is
greater than `worker.nodeGpuMemoryGt` (in MiB). Set `worker.nodeGpuMemoryLt` to
select nodes below a boundary instead; when it is non-empty, the `Lt` boundary
takes precedence over `nodeGpuMemoryGt`. A non-empty `worker.nodeSelector`
takes precedence over both memory-label boundaries, which supports devices such
as GB10 that do not advertise `nvidia.com/gpu.memory`.

Set `model.backendFramework=trtllm` to render a TensorRT-LLM worker. The chart
writes `worker.trtllm.engineConfig` to an ephemeral engine configuration file,
then passes the same `tensor_parallel_size`, `max_batch_size`, and
`max_num_tokens` values to the Dynamo TRT-LLM CLI. This prevents the engine file
and command line from drifting. `worker.trtllm.args` holds additional CLI
arguments as individual array elements.

Set `frontend.nodeSelector` when a large shared worker/frontend image should be
kept off storage-constrained CPU nodes. Selecting the same GPU product as the
worker lets Kubernetes reuse image layers when both components land on one
node; the frontend remains CPU-only and does not request a GPU.

The model cache PVC and Redis data PVC are annotated with
`helm.sh/resource-policy: keep`, so Helm uninstall leaves the storage claims in
place for later reuse or manual cleanup.

## ModelExpress Restart Recovery

`modelexpress.resetRegistryOnStart=true` removes only the selected model's
Redis registry record before ModelExpress reconciles the persistent cache. It
does not remove cached model files. This prevents an interrupted download from
leaving a stale `DOWNLOADING` record that later servers and workers wait on
indefinitely. Disable it only when another ModelExpress server intentionally
shares the same Redis model record and owns the active download.

With `modelexpress.waitForCacheReady=true`, workers wait for the `latest`
snapshot link that ModelExpress creates after a successful download.
ModelExpress invalidates an existing link before exposing its server, then
recreates it only after cache reconciliation succeeds. This keeps workers from
falling back to a second direct Hub download or accepting a stale completion
marker when ModelExpress is restarted, avoiding concurrent writers in the
shared cache.

`worker.applyVllmModelOptLmHeadPatch=true` applies the narrowly scoped,
version-guarded changes from upstream vLLM PR
[#42124](https://github.com/vllm-project/vllm/pull/42124) inside a vLLM 0.22.0
worker container before startup. It enables ModelOpt quantization for a
separately quantized `ParallelLMHead`, passes the quantization config to the
Qwen3.5/3.6 and MTP heads, and accepts scalar scale tensors. The patch fails
closed on another vLLM version or an unexpected source layout. Disable and
remove it after the runtime image moves to vLLM 0.23.0 or another build that
contains PR #42124.
