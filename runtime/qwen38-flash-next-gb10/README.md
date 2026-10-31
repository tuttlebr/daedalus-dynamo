# Qwen3.8 Flash Next on GB10

This runtime serves NVIDIA's unmodified
[`nvidia/Qwen3.8-Flash-Next-NVFP4`](https://huggingface.co/nvidia/Qwen3.8-Flash-Next-NVFP4)
checkpoint through Dynamo. The original FP8 checkpoint has 185,502,232,570
bytes of tensors (172.8 GiB), exceeding the Spark node's 121.5 GiB allocatable
shared CPU/GPU memory before KV, activations, and other workloads.

The NVFP4 checkpoint has 132,639,846,394 bytes of tensors (123.5 GiB), including
its approximately 48 GiB FP8 n-gram embedding table. Quantization alone still
does not fit. `VLLM_PLE_MMAP=1` keeps that table file-backed and gathers only
the rows needed for each step, leaving approximately 76 GiB of resident
weights. Its pages compete with other file cache and can be reclaimed.

Use a local NVMe cache. The values select `local-fast`, RWO, and the GB10 node
for the frontend, ModelExpress, and worker. An NFS cache would put remote page
faults in the generation path. A second Spark worker cannot share this local
cache; this profile intentionally disables autoscaling.

The base is vLLM 0.31.0, pinned by image index digest, with Dynamo 1.6.1 and
ModelExpress 0.7.0. The frontend continues to use NVIDIA's NIXL-enabled runtime,
now Dynamo 1.6.1. The previous PyPI ARM64 bindings had NIXL stubs that could not
construct this model's frontend media loader. Both components use the same
Dynamo version; the worker only needs text-token generation through the TCP
request plane.
The custom build retains the GB10 memory and kernel patches listed below.
`model.runtimeVersionOverride` declares the custom image's Dynamo version to
the operator. The Makefile reads this profile's build Dockerfile, context and
ARM64 platform; existing profiles retain their original build defaults.

The Python files listed in `SHA256SUMS` are unmodified Apache-2.0 source from
[`blazux/qwen3.8-flash-DGX` at a9ec989cdaaa813fcd3755ba87ff6c43ed7c2156](https://github.com/blazux/qwen3.8-flash-DGX/tree/a9ec989cdaaa813fcd3755ba87ff6c43ed7c2156/src).
The Dockerfile applies its disk-backed embedding, prefix-cache block sizing,
exact sparse-attention top-k fallback, and bounded weight-loading fixes.
The FLA shared-memory and warp fixes also follow that revision's Dockerfile.
See [LICENSE](./LICENSE). The deterministic custom CUDA kernel, hybrid
checkpoint conversion, reduced draft vocabulary, and vLLM HTTP parser patches
are not used. Dynamo owns the frontend parsing.

Dynamo 1.6.1 includes the vLLM 0.31 scheduler queue fix. The former local
`patch_dynamo_vllm031.py` adapter has been removed: upstream counts each
request once across `waiting` and `kv_holding_waiting`, preserving
preempted/remote-KV decode classification. The worker image now targets
`1.6.1-vllm0.31.0-1`; rebuild and push it before deploying the updated values.
The 1.6.1 file update has not been deployed; the validation below is historical.

Initial capacity is four sequences, 4,096 batched tokens, 8 GiB KV, and the
native 262,144-token context. The initial profile serves text, reasoning and
tools, matching the repository's existing text-only Spark profile. It omits
the vision encoder and MTP. Piecewise CUDA graphs allow the CPU disk lookups
to execute between captured GPU segments. These are startup settings;
performance and long-context correctness require live measurements.

## Live validation: 2026-10-16

Validated on `daedalus-06` in namespace `daedalus-dynamo`, context
`kubernetes-admin@kubernetes`. The final worker image is
`1.5.1-vllm0.31.0-2`, digest
`sha256:88cb3d3b82a43416c711caf1f51873cd6c8b1e189ea865ffd1e38be5ce102c68`.
The downloaded checkpoint revision is
`fc694b54fb0174e0913e6adf86691ef85a4ead47`.

| Check | Observed result |
| --- | --- |
| Repository tests | 49 passed; Helm lint and server-side dry-run passed |
| Placement and readiness | Frontend, ModelExpress, Redis and one GB10 worker Ready on Spark |
| Model residency | vLLM reported 73.04 GiB model memory; 47.7 GiB PLE table mapped from disk |
| Graphs and KV | Piecewise capture succeeded; 8 GiB KV pool, 589,119 token capacity |
| OpenAI APIs | Catalog, chat, streamed chat, tool call plus tool result, Responses passed |
| Concurrent prefix reuse | Three parallel retrievals returned the correct answer; 9,408 cached tokens observed |
| Long-context retrieval | 250,001 input tokens, correct 8-token answer, 102.64 seconds through the direct frontend |
| Shared gateway | Model advertised; chat and Responses passed at `http://192.168.1.11:32359` |

The final worker had zero restarts through these checks. The node has no swap
configured and retained about 20 GiB available memory during the long request.
Evidence is retained locally under `artifacts/qwen38-flash-next-gb10/` (ignored
by Git). These synthetic functional checks do not establish production
throughput or accuracy. The 262,144-token limit is configured; the longest
tested input was 250,001 tokens. The KV pool holds about 2.25 full-length
contexts, so four sequence slots do not imply four simultaneous full-length
requests without queuing or preemption.

Build natively on Spark from the repository root:

```bash
DOCKER_HOST=ssh://daedalus-06 make image values/qwen/qwen3-8-flash-next-nvfp4/dgx-spark/tp1-pp1-dp1-nvme-offload.yaml
SKIP_BUILD=1 SKIP_PUSH=1 make deploy values/qwen/qwen3-8-flash-next-nvfp4/dgx-spark/tp1-pp1-dp1-nvme-offload.yaml
```

The SSH account needs Docker access. An ARM64 build host or an existing
ARM64-capable builder is also supported. Keep model cache data when removing
the release; the chart retains PVCs.

Validate the rendered deployment and endpoint:

```bash
make lint values/qwen/qwen3-8-flash-next-nvfp4/dgx-spark/tp1-pp1-dp1-nvme-offload.yaml
make status values/qwen/qwen3-8-flash-next-nvfp4/dgx-spark/tp1-pp1-dp1-nvme-offload.yaml
kubectl get pods,pvc -n daedalus-dynamo -o wide
scripts/smoke-test-responses.py --connect-ip 192.168.1.12 \
  --model nvidia/Qwen3.8-Flash-Next-NVFP4
```

Export `OPENAI_API_KEY` using the gateway credential instructions in the
[repository README](../../README.md#deploy-a-model) before running the smoke test.

Use representative prompts before enabling MTP or raising batch capacity.
Check repeated-prefix outputs, concurrent requests, tool calls and memory
pressure before attributing a speed change to a configuration change. Published
results from the patch source are evidence for trying this configuration,
not measurements of this cluster.
