# Official Qwen3.6 FP8 on the existing L40S service

The [staged values](../../values/staging/qwen36-35b-fp8-l40s.yaml) and
[post-renderer](../../scripts/qwen36_fp8_postrender.py) replace the model in the
existing `qwen38-27b-l40s` Helm release. The DGD and frontend Service remain
`qwen38-27b-vllm-l40s` and `qwen38-27b-vllm-l40s-frontend`. The first registered
model is `Qwen/Qwen3.6-35B-A3B-FP8`; the second is the existing
`Qwen/Qwen3.8-27B-FP8` compatibility alias. Confirm both through the real
frontend before accepting the replacement.

This is a staged launch profile. It does not establish usable context capacity,
tool compatibility, application quality or a latency benefit. The old values
file remains the rollback recipe. Existing model caches, Redis, ModelExpress,
gateway routes and unrelated backends remain intact.

## Serving settings and capacity gates

The worker pins the official revision in
[model-pin.json](./model-pin.json), the safetensors loader, FP8 quantization,
the Triton MoE backend and the existing Dynamo 1.4.1 image digest. The worker
and frontend use the existing Python processing and Qwen tool/reasoning parser
combination. There is no speculative decoding or MTP. All consumers stay on
`daedalus-03`; the frontend retains its 4 GiB memory request and 8 GiB limit.

The worker uses one L40S, TP1, eager execution, a 131,072-token model limit,
three sequence slots, prefix caching, chunked prefill and a fixed 4 GiB FP8 KV
pool. The 4,096-token batch budget accommodates the pinned hybrid GDN/attention
page alignment; a 1,024-token budget cannot satisfy the installed backend's
block-size validation. Three slots preserve the repository's active-actor
concurrency requirement. They do not promise three simultaneous 131,072-token
resident contexts: their raw state and aligned KV lower bound already exceeds
the fixed pool. Actual allocation, peak memory, eviction behavior and one full
context must be qualified after startup. Do not shorten the context or change
the pool to make a failed qualification appear successful.

## Prepare and verify the separate cache

Follow [CACHE.md](./CACHE.md) first. Its CPU-only Job downloads the exact pinned
revision and verifies all weight shards against pinned-revision Hub LFS
metadata, plus the configuration, tokenizer, template and remaining repository
files. It publishes an atomic completion marker only after all checks pass.
Preparing that retained claim does not modify the running service or its old
cache.

The post-renderer changes only the existing frontend and worker's model-cache
mounts, environment and startup commands, plus a reference to the new claim.
ModelExpress retains its original claim and configuration. Both serving
components require the completion marker to match the pin; startup rechecks
the six supporting-file hashes, all shard sizes and cache path containment.
It relies on the cache Job's completed full-shard checksum verification rather
than rehashing every weight on each restart. Treat the prepared claim as an
operator-controlled artifact; re-run verification after any cache modification.

Both components use offline Hugging Face/Transformers settings. The worker
loads the snapshot and tokenizer by absolute local path, so a missing or
invalid prepared cache fails before starting the engine. It does not download
an alternative revision or fall back to ModelExpress.

## Review the same-release transition

Use an explicit verified Kubernetes context for every operation. Before any
serving change, preserve the current Helm revision, values and manifest, DGD,
controller identities, old claim UIDs, HTTPRoute/Service identities and working
model aliases. Retain the new and old PVCs throughout the transition. Review
the rendered manifest and server-side dry run against the actual release.

From the repository root, render without touching Kubernetes:

```bash
helm lint charts/dynamo-model -f values/staging/qwen36-35b-fp8-l40s.yaml
helm template qwen38-27b-l40s charts/dynamo-model \
  --namespace daedalus-dynamo \
  -f values/staging/qwen36-35b-fp8-l40s.yaml \
  --post-renderer scripts/qwen36_fp8_postrender.py \
  > /tmp/qwen36-fp8-serving.yaml
```

The normal `make deploy` path also reconciles shared gateway resources and does
not select this post-renderer. Use the explicit same-release command after
reviewed cache and drain gates instead of deploying these values directly.

Replacing a model on one occupied GPU involves a serving outage. Announce it
and verify active and queued model work has drained. A quiet snapshot does not
prevent a new consumer from arriving; identify the affected clients and choose
a controlled window. Fixed local routes may return local errors during the
outage. Do not silently redirect those private requests to a cloud provider.

Pause only this DGD's current worker controller, using its recorded identity,
and wait for the old GPU pod to be fully deleted. Do not pause the operator,
remove shared routes, delete a cache or change an unrelated backend. After the
GPU is free, the reviewed replacement command is:

```bash
helm --kube-context "$EXPECTED_CONTEXT" upgrade qwen38-27b-l40s \
  charts/dynamo-model --namespace daedalus-dynamo \
  -f values/staging/qwen36-35b-fp8-l40s.yaml \
  --post-renderer scripts/qwen36_fp8_postrender.py \
  --wait --timeout 30m
```

Confirm startup uses the exact offline snapshot and pinned image, the backend
reports usable capacity of at least 131,072 tokens, both served model aliases
work, and actual frontend text, automatic/named/required tools and tool-result
continuations work. Recheck existing client paths, storage and unrelated
services. A successful cache check or Kubernetes readiness alone is insufficient
for promotion to the application workflow.

If a qualification fails, retain its evidence, drain and delete only the new
GPU pod through its controller, then restore the captured previous Helm
revision after the GPU is free. Wait for the original backend and verify its
old model ID and existing client paths. Keep both cache claims for recovery;
rolling back serving does not require deleting the new verified artifact.
