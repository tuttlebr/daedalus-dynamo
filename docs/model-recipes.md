# Model recipes and validation

Snapshot: 2026-10-30. The [NVIDIA collection](https://huggingface.co/collections/nvidia/inference-optimized-checkpoints-with-model-optimizer)
contains 93 entries. This checkout provides 113 resident candidate recipes across
39 checkpoints and one specialized Qwen Flash Next NVMe-offload recipe. Every
entry is accounted for below. The initial catalog work did not deploy a new
inference workload; subsequent model-specific tuning is recorded below.

The [2026-11-01 DGX Spark review](dgx-spark-nvidia-recipe-review-2026-11-01.md)
records the original 40 Spark candidates. The subsequent
[NVIDIA Spark implementation](nvidia-spark-recipes.md) adapts ten matching profiles
and adds separate Bolt and Flash Next FP4-Local profiles, for 42 Spark recipes.
Its pinned runtime builds and per-model settings are code only; they have not
been deployed or GPU-qualified. Models without a matching NVIDIA source keep
their existing candidate settings.

## Use a recipe

```bash
make models
make render values/qwen/qwen3-8b-nvfp4/l40s/tp1-pp1-dp1.yaml
make lint values/qwen/qwen3-8b-nvfp4/l40s/tp1-pp1-dp1.yaml
make deploy values/qwen/qwen3-8b-nvfp4/l40s/tp1-pp1-dp1.yaml
```

`make deploy` ensures the shared Model Express runtime ConfigMap, uses the existing
HF token secret, and installs the chosen release. Deploy, install, and upgrade
default to publishing the model through the shared HTTP gateway at
`http://192.168.1.12:32360/v1`, with the existing bearer key. The Service port and
NodePort are fixed at `32360`; HTTPS on `32359` and the internal service on `8000`
remain available for existing clients. `GATEWAY=0` or `recipe.gateway: false`
keeps an explicitly private deployment off the gateway. `GATEWAY=1` overrides
that recipe setting. To connect an already deployed model to the shared gateway:

```bash
make gateway-route-apply values/meta/muse-glimmer-30b-nvfp4/dgx-spark/tp1-pp1-dp1.yaml
make gateway-catalog-apply
```

The model field must match the checkpoint ID; Switchyard callers continue using
their configured route ID, such as `daedalus/hindsight-local`. A model may be
healthy at its private frontend while shared-gateway requests return 404 if its
HTTPRoute has not been installed.

`make runtime-render` previews the one shared ConfigMap;
`make runtime-install` installs it independently. `make upgrade` assumes it already
exists. Runtime code is shared; caches, Redis names and model identities are scoped
to the individual recipe. Uninstall retains PVCs.

The structure is `values/<builder>/<checkpoint>/<hardware>/<parallelism>.yaml`.
`model.yaml` identifies the checkpoint and revision. `_shared/vllm.yaml` contains
serving defaults; `_hardware/` contains placement and platform settings. `extends`
is resolved relative to each file. Later mappings override earlier mappings;
arrays replace rather than append. Use `worker.vllmOptions` for mergeable options.
Changing a parent updates every dependent recipe on its next render/deploy.

Generic candidates inherit `worker.vllmOptions.max-model-len: auto` from the shared
defaults. NVIDIA Spark profiles instead use their published context (including
an omitted limit for Lightning); see their `_tuning.yaml` files.
With `auto`, at worker startup vLLM selects the largest context that fits the
configured memory budget, up to the model's supported maximum. The limit covers
prompt plus generated tokens. Omitting this option instead uses the model's
declared context without requesting automatic memory fitting. See the
[vLLM 0.31 option reference](https://docs.vllm.ai/en/v0.31.0/configuration/engine_args/#max-model-len).
Generated recipes record a separate numeric `recipe.screeningContextLength` for
offline fit estimates and AISimulate. This metadata is stripped before Helm and
does not constrain the runtime's context.

Per-model hardware tuning belongs in `<hardware>/_tuning.yaml`. The catalog
generator includes this layer, uses its screening context and memory fraction for
fit screening, and preserves explicit unified-memory requests. This keeps live
startup fixes intact when regenerating the catalog. Muse on DGX Spark uses this
layer for a 32,768-token screening context, 0.70 GPU memory utilization, an 88 GiB pod memory
request, and worker updates with zero surge / one unavailable replica because
there is only one GPU. Its original 0.90 setting requested 109.46 GiB when only
99.31 GiB was free. Lowering the fraction fixes that startup check; lowering
context alone does not change the requested fraction of total memory. See the
[vLLM memory settings](https://docs.vllm.ai/en/latest/configuration/engine_args/#cacheconfig)
and [Dynamo rollout settings](https://docs.nvidia.com/dynamo/v1.3.0/kubernetes-deployment/operate/rolling-update).

Shared model integration settings belong in `<checkpoint>/_tuning.yaml`. Muse
uses this layer to select the vLLM chat processor and `muse_glimmer` reasoning/tool
parsers, with 8 GiB frontend memory. Without these parsers, reasoning and ATEM
channel markers appeared in `message.content`.

Tuning for just one parallelism configuration belongs in
`<hardware>/_tuning-<parallelism>.yaml`. The generator includes it after the
hardware's `_tuning.yaml` and uses the merged settings for fit screening.
For Muse on one L40S, edit
`values/meta/muse-glimmer-30b-nvfp4/l40s/_tuning-tp1-pp1-dp1.yaml`:
`worker.vllmOptions` contains generation, batching, scheduling, caching and DFlash
settings. A `speculative-config` mapping becomes one JSON command-line argument.
Use `no-enable-flashinfer-autotune: true` to emit that negative flag; `false`
would omit it. TP1 remains in the generated recipe, and tool choice/parsers remain
in the shared Muse `frontend.extraArgs`. The TP2 and Spark recipes do not inherit
this TP1 tuning. This TP1 profile was GPU smoke-tested with DFlash and image
inputs on 2026-11-01; the existing memory screening does not account for
draft-model allocations.

The single-L40S Muse profile uses the versioned runtime in
`runtime/muse-glimmer/`. It preserves Dynamo 1.6.1 and vLLM 0.31.0 and adds Muse's
ATEM opening marker to the tool parser so Dynamo retains the reasoning-to-tool
handoff until the call is complete. The build checks the original parser hash
before patching and supports both amd64 and arm64, preserving the frontend's
existing scheduling options and unprivileged runtime user.
The TP1 tuning overrides the generated `language-model-only` default to `false`,
enabling the perception encoder, and sets `enable-multimodal: true` to enable
Dynamo's request handler. Both settings are required for image requests.
The frontend sets `DYNAMO_DISABLE_NIXL_MM=1` through `frontend.extraEnv`, so the
worker processes the original image instead of receiving a shared-memory segment
that is inaccessible when the frontend and GPU worker run on different nodes.
It also uses zero surge and one unavailable
worker during updates because the other L40S GPUs serve existing consumers.
The model ID, gateway routes, credentials and ports are unchanged.
`runtime/muse-glimmer/test_tool_handoff.py <cached-tokenizer-directory>` exercises
the installed parser with automatic, required and named tool choices, one or two
calls, and one-token, 15-token and whole-response chunks. Run it inside the model
image; it needs only CPU and the cached tokenizer.
See the [deployment investigation](investigations/muse-glimmer-vlm-2026-11-01.md)
for the failure mechanism, live validation and rollback instructions.

Muse Spark was GPU smoke-tested on 2026-10-30 with a fixed context: `/v1/models` reported a 32,768-token
window; short chat and a 31,083-input-token chat request returned the expected
answer with reasoning separated. The long request took 55.47 seconds and generated
145 tokens. The worker remained Ready with zero restarts. These are functional
checks on the text-only recipe, not multimodal or throughput qualification. Logs,
responses, validation metadata and rollback are in `artifacts/muse-spark-20261030/`.
That evidence applies to the earlier fixed limit; the new automatic-context
configuration has not been GPU smoke-tested or deployed as part of this change.

To add a future model, create a model definition inheriting `../../_shared/vllm.yaml`,
then a hardware recipe inheriting `../model.yaml` and the hardware layer. Set unique
`recipe.release`, `model.name`, cache/Redis resource names, pinned model revision,
GPU request and matching TP. Do not put model IDs or endpoints into the shared
ConfigMap. A ConfigMap must be installed in each target namespace. It does not
automatically inject itself into DGDs created outside this chart.

## Hardware and memory policy

| Hardware | Nodes | GPUs per node | Observed memory | Allowed layouts |
|---|---|---:|---:|---|
| DGX Spark / GB10 | daedalus-06 | 1 | 121.5 GiB allocatable unified CPU/GPU RAM | TP1/PP1/DP1 |
| L40S | daedalus-02, daedalus-03 | 2 each | 46,068 MiB per GPU | TP1 or TP2, PP1/DP1 |
| A30 | daedalus-04 | 1 | 24 GiB | TP1/PP1/DP1 |

A two-GPU recipe allocates both GPUs in one worker pod. There is no Ray cluster,
node-spanning pipeline, disaggregation, expert-parallel group or autoscaling.
The hardware inventory initially had only one free L40S and no free L40S pair;
other applications own GPUs independently of the retired Dynamo releases.
Check current allocations before deploying. Node counts are capacity, not a promise
that those GPUs are free.

Resident-fit screening uses actual HF safetensors shard sizes divided by TP,
4 GiB per rank for runtime/repacking/auxiliary tensors, and BF16 KV storage for
one sequence. Offline screening starts at 8,192 tokens (32,768 for the tuned Muse Spark
recipe) and reduces its estimate to 4,096 or 2,048 if
needed, within 90% of the observed memory envelope. Hybrid/sliding attention is
bounded with full-attention KV where geometry is known. Unknown geometry has a
1 GiB placeholder and is explicitly unresolved; these entries require runtime
memory measurement. Sharded weights and replicated multimodal components can make
the estimate differ from actual loading memory. Spark's pod memory request includes
estimated resident GPU memory because its RAM is shared. Model fit never implies
that several recipes fit concurrently. These screening contexts are not serving
limits. Recorded simulation results remain estimates for their original numeric
workloads; they do not establish the context that vLLM will select with `auto`.

Generic candidates are conservative text-serving starting points: batch/concurrency 1, eager
execution, BF16 KV, no speculative decoding. Multimodal wrappers use language-only
mode where supported; image/video/audio capacity is not qualified. NVFP4 uses
vLLM's Marlin path, including on L40S and A30; it is not rejected solely because
those GPUs lack native FP4 tensor cores. See the [vLLM Marlin implementation](https://github.com/vllm-project/vllm/blob/v0.31.0/vllm/model_executor/kernels/linear/nvfp4/marlin.py).

NVIDIA Spark profiles override this policy with model-specific KV precision,
speculation, context, batching and multimodal settings. Their memory requests
reserve the configured fraction of unified RAM plus 4 GiB, and draft checkpoints
have separate revision pins and cache allowances. The generic simulation adapter
does not model these configurations and will not reuse earlier candidate results
as evidence for them.

## AI Simulate evidence

Executed NVIDIA [AISimulate](https://docs.nvidia.com/dynamo/knowledge-base/concepts/simulation/ai-simulate/overview)
0.13.0 from Planner 1.6.1, pinned to image digest
`sha256:7a3f07ce406c988c3c7ce30578fa1880831bb9408454daafdcb0a6f7ad1dabff`.
All 113 resident candidate shapes were attempted or recorded as missing model
configuration. There were 32 successful L40S predictions, 78 unsupported/failed
predictions and 3 gated-config cases. No GPU resources were used for simulation.

The workload was 4,096 input and 512 output tokens, four requests at concurrency 1;
short-context recipes reduce input length to stay inside the configured window.
Pure NVFP4 predictions explicitly use `w4a16_nvfp4`, which models BF16 computation
with packed NVFP4 weights. Native `nvfp4` incorrectly requires FP4 compute on these
cards. Mixed schemes retain their configuration and expose remaining coverage gaps.

These are HYBRID operation-level estimates, not measured throughput or validated
SLOs. The bundled L40S data resolves to vLLM 0.24.0 while the runtime recipes target
0.31.0. Some operations are empirical estimates. GB10 has no bundled system profile;
A30 has hardware metadata but no queryable vLLM performance database. Other failures
include unsupported architectures and missing operation shapes. No H100/GB200
substitution or made-up performance value fills those gaps.

For example, the Llama 3.1 8B NVFP4 L40S estimate gives about 430 ms TTFT / 10.9 ms
TPOT at TP1 and 339 ms / 6.5 ms at TP2. The second GPU improves estimated per-user
latency, but does not double throughput. Both layouts remain available; GPU cost,
actual workload and measured behavior should determine which to deploy.

The [machine-readable results](../config/model-catalog/simulation-results.json)
record each recipe, outcome and available summary. Full inputs, logs and prediction
JSON are in `artifacts/portability-20261030/simulations/`. Re-run with:

```bash
python3 scripts/simulate_model_catalog.py --output artifacts/another-simulation-run
```

The runner caches outcomes in its output directory. Use a new directory after
changing recipes or the image; `--image` can select a different pinned Planner.
Published version metadata in the current report describes the recorded run.

## Validation and retirement

All 114 recipes render and the representative shared ConfigMap/DGDs pass server-side
Kubernetes dry-run validation. Runtime-script tests exercise pinned revision markers,
failed downloads and model-scoped Redis cleanup. Catalog tests check inheritance,
unique names, single-node GPU limits and pins. Before removing the old values paths,
all 18 historical profiles rendered objects identical to their saved counterparts.

The original working tree already had 22 failing assertions and 3 errors in its
128-test suite, largely from restored older values versus in-progress runtime
upgrades. Those historical profiles now live only in regression fixtures. This work
does not silently upgrade their images or erase the existing failures. Current
catalog checks are separate from that historical baseline.

The legacy repository profiles were removed, and the Bolt and private Super Helm
releases were uninstalled as the last cluster changes on 2026-10-30. No legacy DGD,
pod, deployment or service remains. All 15 existing PVCs retain their original UIDs;
the three PVCs belonging to those releases remain Bound. Gateway and HTTPRoute
specifications are unchanged, and the gateway and catalog deployments are available.
HTTPS access to the gateway still returns the expected 401 without credentials;
this check ran from a cluster node, not a separate LAN client. Existing client routes
to the retired model backends have no serving worker until a replacement is selected
and routed. The stack replay script no longer recreates them.

Release values, manifests and final-state evidence are saved in
`artifacts/portability-20261030/teardown/`. Its `rollback.sh` restores the exact saved
manifests using the retained PVCs and original resource names. Helm history is also
retained. No replacement GPU workload was started.

## Complete collection coverage

The linked recipes are candidates. “Estimate” means an offline prediction exists;
“no estimate” is a recorded simulator gap. Draft-only entries require their target
model; Diffusers/ONNX entries require another serving runtime. These are explicit
scope limitations, not claims that their tensors cannot fit in memory.

| Checkpoint | Weight GiB | Recipes / outcome | AI Simulate |
|---|---:|---|---|
| [NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4](https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4) | 20.08 | [dgx-spark/tp1-pp1-dp1](../values/nvidia/nvidia-nemotron-3-5-lightning-30b-a3b-nvfp4/dgx-spark/tp1-pp1-dp1.yaml)<br>[l40s/tp1-pp1-dp1](../values/nvidia/nvidia-nemotron-3-5-lightning-30b-a3b-nvfp4/l40s/tp1-pp1-dp1.yaml)<br>[l40s/tp2-pp1-dp1](../values/nvidia/nvidia-nemotron-3-5-lightning-30b-a3b-nvfp4/l40s/tp2-pp1-dp1.yaml) | no estimate |
| [NVIDIA-Nemotron-3-Super-120B-A12B-NVFP4](https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-NVFP4) | 74.80 | [dgx-spark/tp1-pp1-dp1](../values/nvidia/nvidia-nemotron-3-super-120b-a12b-nvfp4/dgx-spark/tp1-pp1-dp1.yaml) | no estimate |
| [NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4](https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4) | 18.01 | [dgx-spark/tp1-pp1-dp1](../values/nvidia/nvidia-nemotron-3-nano-30b-a3b-nvfp4/dgx-spark/tp1-pp1-dp1.yaml)<br>[l40s/tp1-pp1-dp1](../values/nvidia/nvidia-nemotron-3-nano-30b-a3b-nvfp4/l40s/tp1-pp1-dp1.yaml)<br>[l40s/tp2-pp1-dp1](../values/nvidia/nvidia-nemotron-3-nano-30b-a3b-nvfp4/l40s/tp2-pp1-dp1.yaml) | 2 estimates |
| [NVIDIA-Nemotron-3-Super-120B-A12B-FP8](https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-FP8) | 119.53 | outside conservative memory budget | not simulated |
| [NVIDIA-Nemotron-3-Nano-30B-A3B-FP8](https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-FP8) | 30.44 | [dgx-spark/tp1-pp1-dp1](../values/nvidia/nvidia-nemotron-3-nano-30b-a3b-fp8/dgx-spark/tp1-pp1-dp1.yaml)<br>[l40s/tp1-pp1-dp1](../values/nvidia/nvidia-nemotron-3-nano-30b-a3b-fp8/l40s/tp1-pp1-dp1.yaml)<br>[l40s/tp2-pp1-dp1](../values/nvidia/nvidia-nemotron-3-nano-30b-a3b-fp8/l40s/tp2-pp1-dp1.yaml) | 2 estimates |
| [GLM-5.2-NVFP4](https://huggingface.co/nvidia/GLM-5.2-NVFP4) | 432.90 | too large single node | not simulated |
| [DeepSeek-V4-Pro-NVFP4](https://huggingface.co/nvidia/DeepSeek-V4-Pro-NVFP4) | 850.38 | too large single node | not simulated |
| [Qwen3.5-397B-A17B-NVFP4](https://huggingface.co/nvidia/Qwen3.5-397B-A17B-NVFP4) | 233.94 | too large single node | not simulated |
| [Kimi-K2.6-NVFP4](https://huggingface.co/nvidia/Kimi-K2.6-NVFP4) | 554.31 | too large single node | not simulated |
| [Kimi-K2.5-NVFP4](https://huggingface.co/nvidia/Kimi-K2.5-NVFP4) | 550.24 | too large single node | not simulated |
| [Qwen3-VL-235B-A22B-Instruct-NVFP4](https://huggingface.co/nvidia/Qwen3-VL-235B-A22B-Instruct-NVFP4) | 125.98 | too large single node | not simulated |
| [GLM-5-NVFP4](https://huggingface.co/nvidia/GLM-5-NVFP4) | 429.20 | too large single node | not simulated |
| [MiniMax-M2.7-NVFP4](https://huggingface.co/nvidia/MiniMax-M2.7-NVFP4) | 130.28 | too large single node | not simulated |
| [GLM-5.1-NVFP4](https://huggingface.co/nvidia/GLM-5.1-NVFP4) | 433.89 | too large single node | not simulated |
| [diffusiongemma-26B-A4B-it-NVFP4](https://huggingface.co/nvidia/diffusiongemma-26B-A4B-it-NVFP4) | 17.53 | [dgx-spark/tp1-pp1-dp1](../values/google/diffusiongemma-26b-a4b-it-nvfp4/dgx-spark/tp1-pp1-dp1.yaml)<br>[l40s/tp1-pp1-dp1](../values/google/diffusiongemma-26b-a4b-it-nvfp4/l40s/tp1-pp1-dp1.yaml)<br>[l40s/tp2-pp1-dp1](../values/google/diffusiongemma-26b-a4b-it-nvfp4/l40s/tp2-pp1-dp1.yaml) | no estimate |
| [MiniMax-M3-NVFP4](https://huggingface.co/nvidia/MiniMax-M3-NVFP4) | 232.93 | too large single node | not simulated |
| [MiniMax-M2.5-NVFP4](https://huggingface.co/nvidia/MiniMax-M2.5-NVFP4) | 130.28 | too large single node | not simulated |
| [Gemma-4-31B-IT-NVFP4](https://huggingface.co/nvidia/Gemma-4-31B-IT-NVFP4) | 30.39 | [dgx-spark/tp1-pp1-dp1](../values/google/gemma-4-31b-it-nvfp4/dgx-spark/tp1-pp1-dp1.yaml)<br>[l40s/tp1-pp1-dp1](../values/google/gemma-4-31b-it-nvfp4/l40s/tp1-pp1-dp1.yaml)<br>[l40s/tp2-pp1-dp1](../values/google/gemma-4-31b-it-nvfp4/l40s/tp2-pp1-dp1.yaml) | 1 estimates |
| [Gemma-4-26B-A4B-NVFP4](https://huggingface.co/nvidia/Gemma-4-26B-A4B-NVFP4) | 17.50 | [dgx-spark/tp1-pp1-dp1](../values/google/gemma-4-26b-a4b-nvfp4/dgx-spark/tp1-pp1-dp1.yaml)<br>[l40s/tp1-pp1-dp1](../values/google/gemma-4-26b-a4b-nvfp4/l40s/tp1-pp1-dp1.yaml)<br>[l40s/tp2-pp1-dp1](../values/google/gemma-4-26b-a4b-nvfp4/l40s/tp2-pp1-dp1.yaml) | 2 estimates |
| [Wan2.2-T2V-A14B-Diffusers-NVFP4](https://huggingface.co/nvidia/Wan2.2-T2V-A14B-Diffusers-NVFP4) | 32.18 | requires diffusion runtime | not simulated |
| [Qwen3.6-35B-A3B-NVFP4](https://huggingface.co/nvidia/Qwen3.6-35B-A3B-NVFP4) | 21.82 | [dgx-spark/tp1-pp1-dp1](../values/qwen/qwen3-6-35b-a3b-nvfp4/dgx-spark/tp1-pp1-dp1.yaml)<br>[l40s/tp1-pp1-dp1](../values/qwen/qwen3-6-35b-a3b-nvfp4/l40s/tp1-pp1-dp1.yaml)<br>[l40s/tp2-pp1-dp1](../values/qwen/qwen3-6-35b-a3b-nvfp4/l40s/tp2-pp1-dp1.yaml) | 2 estimates |
| [DeepSeek-R1-0528-NVFP4-v2](https://huggingface.co/nvidia/DeepSeek-R1-0528-NVFP4-v2) | 384.94 | too large single node | not simulated |
| [Kimi-K2-Thinking-NVFP4](https://huggingface.co/nvidia/Kimi-K2-Thinking-NVFP4) | 553.43 | too large single node | not simulated |
| [Qwen3-Next-80B-A3B-Thinking-NVFP4](https://huggingface.co/nvidia/Qwen3-Next-80B-A3B-Thinking-NVFP4) | 47.27 | [dgx-spark/tp1-pp1-dp1](../values/qwen/qwen3-next-80b-a3b-thinking-nvfp4/dgx-spark/tp1-pp1-dp1.yaml)<br>[l40s/tp2-pp1-dp1](../values/qwen/qwen3-next-80b-a3b-thinking-nvfp4/l40s/tp2-pp1-dp1.yaml) | no estimate |
| [Qwen3-Next-80B-A3B-Instruct-NVFP4](https://huggingface.co/nvidia/Qwen3-Next-80B-A3B-Instruct-NVFP4) | 47.27 | [dgx-spark/tp1-pp1-dp1](../values/qwen/qwen3-next-80b-a3b-instruct-nvfp4/dgx-spark/tp1-pp1-dp1.yaml)<br>[l40s/tp2-pp1-dp1](../values/qwen/qwen3-next-80b-a3b-instruct-nvfp4/l40s/tp2-pp1-dp1.yaml) | no estimate |
| [Llama-3.3-70B-Instruct-NVFP4](https://huggingface.co/nvidia/Llama-3.3-70B-Instruct-NVFP4) | 39.78 | [dgx-spark/tp1-pp1-dp1](../values/meta/llama-3-3-70b-instruct-nvfp4/dgx-spark/tp1-pp1-dp1.yaml)<br>[l40s/tp2-pp1-dp1](../values/meta/llama-3-3-70b-instruct-nvfp4/l40s/tp2-pp1-dp1.yaml) | 1 estimates |
| [Llama-3.3-70B-Instruct-FP8](https://huggingface.co/nvidia/Llama-3.3-70B-Instruct-FP8) | 67.67 | [dgx-spark/tp1-pp1-dp1](../values/meta/llama-3-3-70b-instruct-fp8/dgx-spark/tp1-pp1-dp1.yaml)<br>[l40s/tp2-pp1-dp1](../values/meta/llama-3-3-70b-instruct-fp8/l40s/tp2-pp1-dp1.yaml) | 1 estimates |
| [Qwen3-235B-A22B-FP8](https://huggingface.co/nvidia/Qwen3-235B-A22B-FP8) | 220.17 | too large single node | not simulated |
| [Llama-3.1-8B-Instruct-NVFP4](https://huggingface.co/nvidia/Llama-3.1-8B-Instruct-NVFP4) | 5.61 | [dgx-spark/tp1-pp1-dp1](../values/meta/llama-3-1-8b-instruct-nvfp4/dgx-spark/tp1-pp1-dp1.yaml)<br>[l40s/tp1-pp1-dp1](../values/meta/llama-3-1-8b-instruct-nvfp4/l40s/tp1-pp1-dp1.yaml)<br>[l40s/tp2-pp1-dp1](../values/meta/llama-3-1-8b-instruct-nvfp4/l40s/tp2-pp1-dp1.yaml)<br>[a30/tp1-pp1-dp1](../values/meta/llama-3-1-8b-instruct-nvfp4/a30/tp1-pp1-dp1.yaml) | 2 estimates |
| [Llama-3.1-8B-Instruct-FP8](https://huggingface.co/nvidia/Llama-3.1-8B-Instruct-FP8) | 8.46 | [dgx-spark/tp1-pp1-dp1](../values/meta/llama-3-1-8b-instruct-fp8/dgx-spark/tp1-pp1-dp1.yaml)<br>[l40s/tp1-pp1-dp1](../values/meta/llama-3-1-8b-instruct-fp8/l40s/tp1-pp1-dp1.yaml)<br>[l40s/tp2-pp1-dp1](../values/meta/llama-3-1-8b-instruct-fp8/l40s/tp2-pp1-dp1.yaml)<br>[a30/tp1-pp1-dp1](../values/meta/llama-3-1-8b-instruct-fp8/a30/tp1-pp1-dp1.yaml) | 2 estimates |
| [Qwen3-30B-A3B-NVFP4](https://huggingface.co/nvidia/Qwen3-30B-A3B-NVFP4) | 16.85 | [dgx-spark/tp1-pp1-dp1](../values/qwen/qwen3-30b-a3b-nvfp4/dgx-spark/tp1-pp1-dp1.yaml)<br>[l40s/tp1-pp1-dp1](../values/qwen/qwen3-30b-a3b-nvfp4/l40s/tp1-pp1-dp1.yaml)<br>[l40s/tp2-pp1-dp1](../values/qwen/qwen3-30b-a3b-nvfp4/l40s/tp2-pp1-dp1.yaml)<br>[a30/tp1-pp1-dp1](../values/qwen/qwen3-30b-a3b-nvfp4/a30/tp1-pp1-dp1.yaml) | 2 estimates |
| [DeepSeek-R1-0528-NVFP4](https://huggingface.co/nvidia/DeepSeek-R1-0528-NVFP4) | 394.53 | too large single node | not simulated |
| [Qwen3-235B-A22B-NVFP4](https://huggingface.co/nvidia/Qwen3-235B-A22B-NVFP4) | 124.91 | too large single node | not simulated |
| [Qwen3-32B-NVFP4](https://huggingface.co/nvidia/Qwen3-32B-NVFP4) | 19.25 | [dgx-spark/tp1-pp1-dp1](../values/qwen/qwen3-32b-nvfp4/dgx-spark/tp1-pp1-dp1.yaml)<br>[l40s/tp1-pp1-dp1](../values/qwen/qwen3-32b-nvfp4/l40s/tp1-pp1-dp1.yaml)<br>[l40s/tp2-pp1-dp1](../values/qwen/qwen3-32b-nvfp4/l40s/tp2-pp1-dp1.yaml) | 2 estimates |
| [Qwen3-14B-NVFP4](https://huggingface.co/nvidia/Qwen3-14B-NVFP4) | 9.82 | [dgx-spark/tp1-pp1-dp1](../values/qwen/qwen3-14b-nvfp4/dgx-spark/tp1-pp1-dp1.yaml)<br>[l40s/tp1-pp1-dp1](../values/qwen/qwen3-14b-nvfp4/l40s/tp1-pp1-dp1.yaml)<br>[l40s/tp2-pp1-dp1](../values/qwen/qwen3-14b-nvfp4/l40s/tp2-pp1-dp1.yaml)<br>[a30/tp1-pp1-dp1](../values/qwen/qwen3-14b-nvfp4/a30/tp1-pp1-dp1.yaml) | 2 estimates |
| [Nemotron-3-Nano-Omni-30B-A3B-Reasoning-FP8](https://huggingface.co/nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-FP8) | 32.77 | [dgx-spark/tp1-pp1-dp1](../values/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning-fp8/dgx-spark/tp1-pp1-dp1.yaml)<br>[l40s/tp1-pp1-dp1](../values/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning-fp8/l40s/tp1-pp1-dp1.yaml)<br>[l40s/tp2-pp1-dp1](../values/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning-fp8/l40s/tp2-pp1-dp1.yaml) | no estimate |
| [Qwen3-14B-FP8](https://huggingface.co/nvidia/Qwen3-14B-FP8) | 15.20 | [dgx-spark/tp1-pp1-dp1](../values/qwen/qwen3-14b-fp8/dgx-spark/tp1-pp1-dp1.yaml)<br>[l40s/tp1-pp1-dp1](../values/qwen/qwen3-14b-fp8/l40s/tp1-pp1-dp1.yaml)<br>[l40s/tp2-pp1-dp1](../values/qwen/qwen3-14b-fp8/l40s/tp2-pp1-dp1.yaml)<br>[a30/tp1-pp1-dp1](../values/qwen/qwen3-14b-fp8/a30/tp1-pp1-dp1.yaml) | 2 estimates |
| [Qwen3-8B-NVFP4](https://huggingface.co/nvidia/Qwen3-8B-NVFP4) | 5.96 | [dgx-spark/tp1-pp1-dp1](../values/qwen/qwen3-8b-nvfp4/dgx-spark/tp1-pp1-dp1.yaml)<br>[l40s/tp1-pp1-dp1](../values/qwen/qwen3-8b-nvfp4/l40s/tp1-pp1-dp1.yaml)<br>[l40s/tp2-pp1-dp1](../values/qwen/qwen3-8b-nvfp4/l40s/tp2-pp1-dp1.yaml)<br>[a30/tp1-pp1-dp1](../values/qwen/qwen3-8b-nvfp4/a30/tp1-pp1-dp1.yaml) | 2 estimates |
| [Qwen3-8B-FP8](https://huggingface.co/nvidia/Qwen3-8B-FP8) | 8.79 | [dgx-spark/tp1-pp1-dp1](../values/qwen/qwen3-8b-fp8/dgx-spark/tp1-pp1-dp1.yaml)<br>[l40s/tp1-pp1-dp1](../values/qwen/qwen3-8b-fp8/l40s/tp1-pp1-dp1.yaml)<br>[l40s/tp2-pp1-dp1](../values/qwen/qwen3-8b-fp8/l40s/tp2-pp1-dp1.yaml)<br>[a30/tp1-pp1-dp1](../values/qwen/qwen3-8b-fp8/a30/tp1-pp1-dp1.yaml) | 2 estimates |
| [Llama-3_3-Nemotron-Super-49B-v1_5-FP8](https://huggingface.co/nvidia/Llama-3_3-Nemotron-Super-49B-v1_5-FP8) | 48.40 | [dgx-spark/tp1-pp1-dp1](../values/nvidia/llama-3-3-nemotron-super-49b-v1-5-fp8/dgx-spark/tp1-pp1-dp1.yaml)<br>[l40s/tp2-pp1-dp1](../values/nvidia/llama-3-3-nemotron-super-49b-v1-5-fp8/l40s/tp2-pp1-dp1.yaml) | no estimate |
| [Llama-3_1-Nemotron-Ultra-253B-v1-FP8](https://huggingface.co/nvidia/Llama-3_1-Nemotron-Ultra-253B-v1-FP8) | 239.91 | too large single node | not simulated |
| [Llama-4-Scout-17B-16E-Instruct-NVFP4](https://huggingface.co/nvidia/Llama-4-Scout-17B-16E-Instruct-NVFP4) | 60.86 | [dgx-spark/tp1-pp1-dp1](../values/meta/llama-4-scout-17b-16e-instruct-nvfp4/dgx-spark/tp1-pp1-dp1.yaml)<br>[l40s/tp2-pp1-dp1](../values/meta/llama-4-scout-17b-16e-instruct-nvfp4/l40s/tp2-pp1-dp1.yaml) | no estimate |
| [Llama-4-Scout-17B-16E-Instruct-FP8](https://huggingface.co/nvidia/Llama-4-Scout-17B-16E-Instruct-FP8) | 103.92 | [dgx-spark/tp1-pp1-dp1](../values/meta/llama-4-scout-17b-16e-instruct-fp8/dgx-spark/tp1-pp1-dp1.yaml) | no estimate |
| [Llama-4-Maverick-17B-128E-Instruct-FP8](https://huggingface.co/nvidia/Llama-4-Maverick-17B-128E-Instruct-FP8) | 376.76 | too large single node | not simulated |
| [Phi-4-reasoning-plus-FP8](https://huggingface.co/nvidia/Phi-4-reasoning-plus-FP8) | 14.61 | [dgx-spark/tp1-pp1-dp1](../values/microsoft/phi-4-reasoning-plus-fp8/dgx-spark/tp1-pp1-dp1.yaml)<br>[l40s/tp1-pp1-dp1](../values/microsoft/phi-4-reasoning-plus-fp8/l40s/tp1-pp1-dp1.yaml)<br>[l40s/tp2-pp1-dp1](../values/microsoft/phi-4-reasoning-plus-fp8/l40s/tp2-pp1-dp1.yaml)<br>[a30/tp1-pp1-dp1](../values/microsoft/phi-4-reasoning-plus-fp8/a30/tp1-pp1-dp1.yaml) | no estimate |
| [Qwen2.5-VL-7B-Instruct-NVFP4](https://huggingface.co/nvidia/Qwen2.5-VL-7B-Instruct-NVFP4) | 6.71 | [dgx-spark/tp1-pp1-dp1](../values/qwen/qwen2-5-vl-7b-instruct-nvfp4/dgx-spark/tp1-pp1-dp1.yaml)<br>[l40s/tp1-pp1-dp1](../values/qwen/qwen2-5-vl-7b-instruct-nvfp4/l40s/tp1-pp1-dp1.yaml)<br>[l40s/tp2-pp1-dp1](../values/qwen/qwen2-5-vl-7b-instruct-nvfp4/l40s/tp2-pp1-dp1.yaml)<br>[a30/tp1-pp1-dp1](../values/qwen/qwen2-5-vl-7b-instruct-nvfp4/a30/tp1-pp1-dp1.yaml) | no estimate |
| [FLUX.1-dev-onnx](https://huggingface.co/black-forest-labs/FLUX.1-dev-onnx) | 0.00 | requires diffusion runtime | not simulated |
| [DeepSeek-R1-NVFP4](https://huggingface.co/nvidia/DeepSeek-R1-NVFP4) | 394.53 | too large single node | not simulated |
| [DeepSeek-R1-NVFP4-v2](https://huggingface.co/nvidia/DeepSeek-R1-NVFP4-v2) | 384.94 | too large single node | not simulated |
| [DeepSeek-V3-0324-NVFP4](https://huggingface.co/nvidia/DeepSeek-V3-0324-NVFP4) | 394.53 | too large single node | not simulated |
| [Llama-3_3-Nemotron-Super-49B-v1-FP8](https://huggingface.co/nvidia/Llama-3_3-Nemotron-Super-49B-v1-FP8) | 48.40 | [dgx-spark/tp1-pp1-dp1](../values/nvidia/llama-3-3-nemotron-super-49b-v1-fp8/dgx-spark/tp1-pp1-dp1.yaml)<br>[l40s/tp2-pp1-dp1](../values/nvidia/llama-3-3-nemotron-super-49b-v1-fp8/l40s/tp2-pp1-dp1.yaml) | no estimate |
| [Llama-3.1-70B-Instruct-FP8](https://huggingface.co/nvidia/Llama-3.1-70B-Instruct-FP8) | 67.67 | [dgx-spark/tp1-pp1-dp1](../values/meta/llama-3-1-70b-instruct-fp8/dgx-spark/tp1-pp1-dp1.yaml)<br>[l40s/tp2-pp1-dp1](../values/meta/llama-3-1-70b-instruct-fp8/l40s/tp2-pp1-dp1.yaml) | 1 estimates |
| [Llama-3.1-405B-Instruct-FP8](https://huggingface.co/nvidia/Llama-3.1-405B-Instruct-FP8) | 381.90 | too large single node | not simulated |
| [Llama-3.1-405B-Instruct-NVFP4](https://huggingface.co/nvidia/Llama-3.1-405B-Instruct-NVFP4) | 218.25 | too large single node | not simulated |
| [Phi-4-multimodal-instruct-NVFP4](https://huggingface.co/nvidia/Phi-4-multimodal-instruct-NVFP4) | 6.76 | [dgx-spark/tp1-pp1-dp1](../values/microsoft/phi-4-multimodal-instruct-nvfp4/dgx-spark/tp1-pp1-dp1.yaml)<br>[l40s/tp1-pp1-dp1](../values/microsoft/phi-4-multimodal-instruct-nvfp4/l40s/tp1-pp1-dp1.yaml)<br>[l40s/tp2-pp1-dp1](../values/microsoft/phi-4-multimodal-instruct-nvfp4/l40s/tp2-pp1-dp1.yaml)<br>[a30/tp1-pp1-dp1](../values/microsoft/phi-4-multimodal-instruct-nvfp4/a30/tp1-pp1-dp1.yaml) | no estimate |
| [Phi-4-multimodal-instruct-FP8](https://huggingface.co/nvidia/Phi-4-multimodal-instruct-FP8) | 8.07 | [dgx-spark/tp1-pp1-dp1](../values/microsoft/phi-4-multimodal-instruct-fp8/dgx-spark/tp1-pp1-dp1.yaml)<br>[l40s/tp1-pp1-dp1](../values/microsoft/phi-4-multimodal-instruct-fp8/l40s/tp1-pp1-dp1.yaml)<br>[l40s/tp2-pp1-dp1](../values/microsoft/phi-4-multimodal-instruct-fp8/l40s/tp2-pp1-dp1.yaml)<br>[a30/tp1-pp1-dp1](../values/microsoft/phi-4-multimodal-instruct-fp8/a30/tp1-pp1-dp1.yaml) | no estimate |
| [Phi-4-reasoning-plus-NVFP4](https://huggingface.co/nvidia/Phi-4-reasoning-plus-NVFP4) | 9.06 | [dgx-spark/tp1-pp1-dp1](../values/microsoft/phi-4-reasoning-plus-nvfp4/dgx-spark/tp1-pp1-dp1.yaml)<br>[l40s/tp1-pp1-dp1](../values/microsoft/phi-4-reasoning-plus-nvfp4/l40s/tp1-pp1-dp1.yaml)<br>[l40s/tp2-pp1-dp1](../values/microsoft/phi-4-reasoning-plus-nvfp4/l40s/tp2-pp1-dp1.yaml)<br>[a30/tp1-pp1-dp1](../values/microsoft/phi-4-reasoning-plus-nvfp4/a30/tp1-pp1-dp1.yaml) | no estimate |
| [Qwen2.5-VL-7B-Instruct-FP8](https://huggingface.co/nvidia/Qwen2.5-VL-7B-Instruct-FP8) | 9.37 | [dgx-spark/tp1-pp1-dp1](../values/qwen/qwen2-5-vl-7b-instruct-fp8/dgx-spark/tp1-pp1-dp1.yaml)<br>[l40s/tp1-pp1-dp1](../values/qwen/qwen2-5-vl-7b-instruct-fp8/l40s/tp1-pp1-dp1.yaml)<br>[l40s/tp2-pp1-dp1](../values/qwen/qwen2-5-vl-7b-instruct-fp8/l40s/tp2-pp1-dp1.yaml)<br>[a30/tp1-pp1-dp1](../values/qwen/qwen2-5-vl-7b-instruct-fp8/a30/tp1-pp1-dp1.yaml) | no estimate |
| [DeepSeek-V3.1-NVFP4](https://huggingface.co/nvidia/DeepSeek-V3.1-NVFP4) | 384.94 | too large single node | not simulated |
| [DeepSeek-V3.2-NVFP4](https://huggingface.co/nvidia/DeepSeek-V3.2-NVFP4) | 386.56 | too large single node | not simulated |
| [Qwen3-Coder-480B-A35B-Instruct-NVFP4](https://huggingface.co/nvidia/Qwen3-Coder-480B-A35B-Instruct-NVFP4) | 254.13 | too large single node | not simulated |
| [Qwen3-235B-A22B-Thinking-2507-NVFP4](https://huggingface.co/nvidia/Qwen3-235B-A22B-Thinking-2507-NVFP4) | 129.66 | too large single node | not simulated |
| [Qwen3-235B-A22B-Instruct-2507-NVFP4](https://huggingface.co/nvidia/Qwen3-235B-A22B-Instruct-2507-NVFP4) | 129.66 | too large single node | not simulated |
| [GLM-4.7-NVFP4](https://huggingface.co/nvidia/GLM-4.7-NVFP4) | 214.13 | too large single node | not simulated |
| [Nemotron-3-Nano-Omni-30B-A3B-Reasoning-NVFP4](https://huggingface.co/nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-NVFP4) | 20.87 | [dgx-spark/tp1-pp1-dp1](../values/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning-nvfp4/dgx-spark/tp1-pp1-dp1.yaml)<br>[l40s/tp1-pp1-dp1](../values/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning-nvfp4/l40s/tp1-pp1-dp1.yaml)<br>[l40s/tp2-pp1-dp1](../values/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning-nvfp4/l40s/tp2-pp1-dp1.yaml) | no estimate |
| [Wan2.2-T2V-A14B-Diffusers-FP8](https://huggingface.co/nvidia/Wan2.2-T2V-A14B-Diffusers-FP8) | 41.91 | requires diffusion runtime | not simulated |
| [Step-3.7-Flash-NVFP4](https://huggingface.co/stepfun-ai/Step-3.7-Flash-NVFP4) | 120.37 | outside conservative memory budget | not simulated |
| [Qwen3.5-122B-A10B-NVFP4](https://huggingface.co/nvidia/Qwen3.5-122B-A10B-NVFP4) | 77.76 | [dgx-spark/tp1-pp1-dp1](../values/qwen/qwen3-5-122b-a10b-nvfp4/dgx-spark/tp1-pp1-dp1.yaml) | no estimate |
| [NVIDIA-Nemotron-3-Ultra-550B-A55B-NVFP4](https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Ultra-550B-A55B-NVFP4) | 328.11 | too large single node | not simulated |
| [DeepSeek-V4-Flash-NVFP4](https://huggingface.co/nvidia/DeepSeek-V4-Flash-NVFP4) | 156.72 | too large single node | not simulated |
| [Qwen3.6-27B-NVFP4](https://huggingface.co/nvidia/Qwen3.6-27B-NVFP4) | 20.42 | [dgx-spark/tp1-pp1-dp1](../values/qwen/qwen3-6-27b-nvfp4/dgx-spark/tp1-pp1-dp1.yaml)<br>[l40s/tp1-pp1-dp1](../values/qwen/qwen3-6-27b-nvfp4/l40s/tp1-pp1-dp1.yaml)<br>[l40s/tp2-pp1-dp1](../values/qwen/qwen3-6-27b-nvfp4/l40s/tp2-pp1-dp1.yaml) | 2 estimates |
| [Mistral-Medium-3.5-128B-NVFP4](https://huggingface.co/nvidia/Mistral-Medium-3.5-128B-NVFP4) | 88.69 | [dgx-spark/tp1-pp1-dp1](../values/mistralai/mistral-medium-3-5-128b-nvfp4/dgx-spark/tp1-pp1-dp1.yaml) | no estimate |
| [Qwen3.5-397B-A17B-NVFP4-V2](https://huggingface.co/nvidia/Qwen3.5-397B-A17B-NVFP4-V2) | 226.85 | too large single node | not simulated |
| [Kimi-K2.7-Code-NVFP4](https://huggingface.co/nvidia/Kimi-K2.7-Code-NVFP4) | 554.31 | too large single node | not simulated |
| [Inkling-NVFP4](https://huggingface.co/thinkingmachines/Inkling-NVFP4) | 551.35 | too large single node | not simulated |
| [Qwen-Image-Flash](https://huggingface.co/nvidia/Qwen-Image-Flash) | 53.74 | requires diffusion runtime | not simulated |
| [Kimi-K3-NVFP4](https://huggingface.co/nvidia/Kimi-K3-NVFP4) | 1499.35 | too large single node | not simulated |
| [K-EXAONE-2.0-750B-A37B-NVFP4](https://huggingface.co/LGAI-EXAONE/K-EXAONE-2.0-750B-A37B-NVFP4) | 490.19 | too large single node | not simulated |
| [Qwen3.8-2.4T-A95B-NVFP4](https://huggingface.co/nvidia/Qwen3.8-2.4T-A95B-NVFP4) | 1345.28 | too large single node | not simulated |
| [DeepSeek-V4-Pro-0813-NVFP4](https://huggingface.co/nvidia/DeepSeek-V4-Pro-0813-NVFP4) | 876.48 | too large single node | not simulated |
| [DeepSeek-V4-Flash-0731-NVFP4](https://huggingface.co/nvidia/DeepSeek-V4-Flash-0731-NVFP4) | 163.49 | too large single node | not simulated |
| [Muse-Glimmer-30B-NVFP4](https://huggingface.co/nvidia/Muse-Glimmer-30B-NVFP4) | 22.98 | [dgx-spark/tp1-pp1-dp1](../values/meta/muse-glimmer-30b-nvfp4/dgx-spark/tp1-pp1-dp1.yaml)<br>[l40s/tp1-pp1-dp1](../values/meta/muse-glimmer-30b-nvfp4/l40s/tp1-pp1-dp1.yaml)<br>[l40s/tp2-pp1-dp1](../values/meta/muse-glimmer-30b-nvfp4/l40s/tp2-pp1-dp1.yaml) | 2 estimates |
| [Qwen3.8-Flash-Next-NVFP4](https://huggingface.co/nvidia/Qwen3.8-Flash-Next-NVFP4) | 123.57 | [dgx-spark/tp1-pp1-dp1-nvme-offload](../values/qwen/qwen3-8-flash-next-nvfp4/dgx-spark/tp1-pp1-dp1-nvme-offload.yaml) | no estimate |
| [Qwen3.8-27B-NVFP4](https://huggingface.co/nvidia/Qwen3.8-27B-NVFP4) | 20.42 | [dgx-spark/tp1-pp1-dp1](../values/qwen/qwen3-8-27b-nvfp4/dgx-spark/tp1-pp1-dp1.yaml)<br>[l40s/tp1-pp1-dp1](../values/qwen/qwen3-8-27b-nvfp4/l40s/tp1-pp1-dp1.yaml)<br>[l40s/tp2-pp1-dp1](../values/qwen/qwen3-8-27b-nvfp4/l40s/tp2-pp1-dp1.yaml) | no estimate |
| [GLM-5.3-Flash-NVFP4](https://huggingface.co/nvidia/GLM-5.3-Flash-NVFP4) | 190.40 | too large single node | not simulated |
| [DeepSeek-V4.1-Flash-NVFP4](https://huggingface.co/nvidia/DeepSeek-V4.1-Flash-NVFP4) | 399.52 | too large single node | not simulated |
| [GLM-5.3-NVFP4](https://huggingface.co/nvidia/GLM-5.3-NVFP4) | 432.27 | too large single node | not simulated |
| [Qwen-Image-NVFP4](https://huggingface.co/nvidia/Qwen-Image-NVFP4) | 28.27 | requires diffusion runtime | not simulated |
| [NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4](https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4) | 69.86 | [dgx-spark/tp1-pp1-dp1](../values/nvidia/nvidia-nemotron-3-5-super-vl-120b-a12b-nvfp4/dgx-spark/tp1-pp1-dp1.yaml)<br>[l40s/tp2-pp1-dp1](../values/nvidia/nvidia-nemotron-3-5-super-vl-120b-a12b-nvfp4/l40s/tp2-pp1-dp1.yaml) | no estimate |
| [NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-BF16](https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-BF16) | 231.82 | too large single node | not simulated |
| [NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4-LiLiCorr](https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4-LiLiCorr) | 1.23 | draft only | not simulated |
| [Step-5-Preview-NVFP4](https://huggingface.co/stepfun-ai/Step-5-Preview-NVFP4) | 367.10 | too large single node | not simulated |
| [Muse-Spark-Open-816B-A42B-DFlash2](https://huggingface.co/nvidia/Muse-Spark-Open-816B-A42B-DFlash2) | 8.17 | draft only | not simulated |
