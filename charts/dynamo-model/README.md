# Dynamo Model Helm Chart

This chart renders one Dynamo model backend release from a model values file.
It is intended to replace the repeated per-model Kubernetes YAML once the Helm
path is proven in the cluster.

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
