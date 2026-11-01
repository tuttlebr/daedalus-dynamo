# NVIDIA recipes for one 128 GB DGX Spark

Implemented as code on 2026-11-01. **No deployments were changed.** These profiles
adapt NVIDIA's published settings to this repository's Kubernetes and Dynamo
1.6.1 serving model. They require image builds and GPU qualification before use;
Helm rendering is not runtime or performance qualification.

The source is [NVIDIA local-ai-inference-recipes at v0.66](https://github.com/NVIDIA/local-ai-inference-recipes/tree/7b2ee453234617f58e1e100c195ac3be7f79e233).
[The checked-in source lock](../config/model-catalog/nvidia-spark.json) records
exact source paths/hashes, normalized options, image digests, engine versions,
and target/draft revisions. Ten existing Spark profiles now use those settings.
Two new, initially private profiles cover Bolt and Flash Next FP4-Local.
The 29 candidates without a matching single-Spark source and the older Flash Next
NVFP4/offload profile retain their previous settings. L40S and A30 profiles are
unchanged.

| Model | Values | Runtime family | Context | GPU fraction | Worker RAM request |
|---|---|---|---|---:|---:|
| diffusiongemma-26B-A4B-it-NVFP4 | [recipe](../values/google/diffusiongemma-26b-a4b-it-nvfp4/dgx-spark/tp1-pp1-dp1.yaml) | `spark-20261014` | `262144` | 0.8 | 102Gi |
| Gemma-4-26B-A4B-NVFP4 | [recipe](../values/google/gemma-4-26b-a4b-nvfp4/dgx-spark/tp1-pp1-dp1.yaml) | `spark-20261016` | `262144` | 0.8 | 102Gi |
| Gemma-4-31B-IT-NVFP4 | [recipe](../values/google/gemma-4-31b-it-nvfp4/dgx-spark/tp1-pp1-dp1.yaml) | `spark-20261016` | `262144` | 0.8 | 102Gi |
| NVIDIA-Nemotron-3-Super-120B-A12B-NVFP4 | [recipe](../values/nvidia/nvidia-nemotron-3-super-120b-a12b-nvfp4/dgx-spark/tp1-pp1-dp1.yaml) | `vllm-031` | `262144` | 0.8 | 102Gi |
| NVIDIA-Nemotron-3.5-Bolt-74B-A9B-24GB | [recipe](../values/nvidia/nvidia-nemotron-3-5-bolt-74b-a9b-24gb/dgx-spark/tp1-pp1-dp1.yaml) | `spark-20261015` | `auto` | 0.8 | 102Gi |
| NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4 | [recipe](../values/nvidia/nvidia-nemotron-3-5-lightning-30b-a3b-nvfp4/dgx-spark/tp1-pp1-dp1.yaml) | `spark-20261016` | `engine default` | 0.8 | 102Gi |
| NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4 | [recipe](../values/nvidia/nvidia-nemotron-3-5-super-vl-120b-a12b-nvfp4/dgx-spark/tp1-pp1-dp1.yaml) | `spark-20261015` | `auto` | 0.8 | 102Gi |
| Qwen3.5-122B-A10B-NVFP4 | [recipe](../values/qwen/qwen3-5-122b-a10b-nvfp4/dgx-spark/tp1-pp1-dp1.yaml) | `vllm-031` | `262144` | 0.8 | 102Gi |
| Qwen3.6-27B-NVFP4 | [recipe](../values/qwen/qwen3-6-27b-nvfp4/dgx-spark/tp1-pp1-dp1.yaml) | `vllm-031` | `262144` | 0.5 | 65Gi |
| Qwen3.6-35B-A3B-NVFP4 | [recipe](../values/qwen/qwen3-6-35b-a3b-nvfp4/dgx-spark/tp1-pp1-dp1.yaml) | `ngc-2610` | `262144` | 0.8 | 102Gi |
| Qwen3.8-27B-NVFP4 | [recipe](../values/qwen/qwen3-8-27b-nvfp4/dgx-spark/tp1-pp1-dp1.yaml) | `ngc-2610` | `262144` | 0.6 | 77Gi |
| Qwen3.8-Flash-Next-FP4-Local | [recipe](../values/qwen/qwen3-8-flash-next-fp4-local/dgx-spark/tp1-pp1-dp1.yaml) | `b12x-20261015` | `262144` | 0.8 | 102Gi |

Lightning's only published single-Spark profile has a 64 GB minimum. It is
capacity-compatible with this 128 GB machine, but is not a distinct 128 GB
benchmark. Where NVIDIA publishes both capacities, the 128 GB recipe is used.

The adaptations are explicit:

- A single DGD worker requests one GB10 on ARM64 with TP1/PP1/DP1. Planner and
  autoscaling remain disabled. Worker rollouts use zero surge, because the node
  has only one GPU. Model IDs, release names, cache/Redis identities and gateway
  policy of the ten existing profiles are retained.
- Dynamo owns HTTP ingress, the frontend and worker transport. NVIDIA's Docker
  host networking and port flags are replaced by the existing ClusterIP services
  and shared gateway; HTTP `192.168.1.12:32360`, HTTPS `:32359`, internal port
  `8000`, authentication and routes are unchanged.
- Tool parsing, automatic tool choice and default chat-template kwargs go to
  `frontend.extraArgs` with `--dyn-chat-processor vllm`. The reasoning parser is
  also passed to the worker's structured-output engine. Worker `dyn-*` parser
  flags advertise the same parser choices in Dynamo's model metadata.
- Multimodal families enable Dynamo's aggregated multimodal path and no longer
  inherit language-only mode. Their native Dynamo bindings are copied from the
  pinned NVIDIA runtime image, including private media libraries, rather than
  relying on potentially reduced-feature PyPI ARM64 bindings. Frontend and
  worker use the same derivative image, preserving their vLLM parser/model APIs.
- The published GPU fractions, context, sequence/batch limits, KV dtype,
  speculative configurations, Mamba settings, loaders, kernels and environment
  are per-model overrides. Generic eager mode, forced Marlin and other candidate
  defaults are removed when upstream does not set them. An omitted source option
  uses that pinned engine's default, not another model's setting.
- External draft models are downloaded at full commit revisions into the same
  cache before the worker starts. `speculative-config.model` points to that exact
  local snapshot. In-checkpoint MTP needs no external draft. Existing target
  revisions are retained; new targets are pinned. Cache capacity includes drafts.
- Worker memory requests reserve the configured fraction of the observed
  121.53 GiB unified-memory pool, rounded up, plus 4 GiB for CPU overhead. Each
  frontend requests 4 GiB with an 8 GiB limit. These reservations prevent treating
  GPU RAM as independent host capacity; they do not prove startup fit alongside
  current CPU workloads. `screeningContextLength` remains an offline estimate,
  separate from the published serving context. Full-length concurrency and
  loading/compilation peaks still require measurement.
- Worker shared memory is 16 GiB. IPC_LOCK/SYS_RESOURCE permit the published
  unlimited memlock and 64 MiB stack limits. Only the B12x Flash Next profile uses
  unconfined seccomp/AppArmor, matching its io_uring setup; none uses privileged
  mode or host networking.

Flash Next FP4-Local has a new model/release/cache identity. Its local-fast RWO
cache is co-located with every consumer on GB10. Only its four B12x/runtime cache
subdirectories are mounted into the worker; the NGC profile's prebuilt caches
under `/root/.cache` are not hidden. The older NVFP4/mmap profile stays available
under its original path. Bolt also has a new private release; it does not replace
the historical Bolt deployment, compatibility aliases or retained routes.

Six [runtime Dockerfiles](../runtime/nvidia-spark/) use digest-pinned NVIDIA or
community bases from the recipe catalog. They add Dynamo without installing the
`[vllm]` extra, constrain the existing Torch/vLLM/FlashInfer/CUDA/Transformers stack,
and verify those versions after installation. Super VL replaces the mutable
`latest` tag with the pinned 2026-10-15 image corresponding to NVIDIA's recorded
engine version. Version checks fail the build if that assumption is not met.

A narrow, hash-guarded adapter defers Dynamo 1.6.1's unused embedding-process
import when newer vLLM removes its helper. It does not alias changed APIs or
modify generation behavior; these images are for generation, not the embedding
process-pool feature. Each build checks native binding integrity, the engine
version, required kernel configuration and both Dynamo CLI parsers without
loading weights. The same contract check runs on frontend/worker startup.
Qwen3.8-27B inherits the NGC build containing the DFlash2 prefix-cache fix and
uses the published 0.60 memory fraction and dynamic NVFP4 configuration.

Build contexts have Dockerfile-specific allowlists containing only these runtime
scripts and the source lock. Repository secrets, datasets and artifacts are not
sent to the builder. The native donor and engine image must both be ARM64.

For a local preview:

```bash
make lint values/qwen/qwen3-8-27b-nvfp4/dgx-spark/tp1-pp1-dp1.yaml
make render values/qwen/qwen3-8-27b-nvfp4/dgx-spark/tp1-pp1-dp1.yaml
make -n build values/qwen/qwen3-8-27b-nvfp4/dgx-spark/tp1-pp1-dp1.yaml
```

When an image build is desired, use an ARM64-capable builder. This command builds
one runtime image only; it does not push it or change Kubernetes:

```bash
DOCKER_HOST=ssh://daedalus-06 make build values/qwen/qwen3-8-27b-nvfp4/dgx-spark/tp1-pp1-dp1.yaml
```

Models in the same runtime family share an image/tag. A change to its build
inputs must bump that family's output tag in every dependent `_tuning.yaml`.
The final derivative image has no recorded digest until built/pushed; the base
and donor images are already pinned. GPU qualification must cover startup,
actual context, repeated prefix-cache requests, reasoning, structured/streaming
tool calls and enabled media before publishing a new profile.

Durable tuning lives in each model's `dgx-spark/_tuning.yaml` and the common
`values/_shared/nvidia-spark.yaml`. `scripts/build_model_catalog.py` resolves the
full tuning layer, retains memory/cache overrides and registers the two
supplemental recipes without changing the historical HF collection snapshot.
It does not rewrite the NVIDIA source lock or overwrite hand-authored tuning.
Regression tests compare rendered worker/frontend arguments against every
explicit published option, verify pins and draft preparation, and regenerate
the catalog in isolation to protect those settings and other hardware profiles.
The generic AISimulate adapter explicitly declines these specialized profiles,
including when an older generic result exists at the same output path.

Static validation completed: all 42 Spark recipes pass Helm lint; all 12 NVIDIA
profiles render with valid frontend/worker Bash; nine NVIDIA conformance tests
and seven portability tests pass. All six Dockerfile build checks pass. The full
repository suite reports 22 failures and three errors, with the exact same set
reproduced in a pristine archive of the pre-change commit; no new failures or
errors were introduced. Python compilation, repository hygiene and secret checks
also pass. Ruff was unavailable. These checks do not execute image installation,
load model weights or validate GPU behavior.

The pre-change local configuration backup is
`artifacts/nvidia-spark-implementation-20261101/before.tar.gz`. It includes the
values, chart, generator, simulation adapter, catalog and earlier recipe docs.
No live backup or rollback was needed because no live configuration changed.
