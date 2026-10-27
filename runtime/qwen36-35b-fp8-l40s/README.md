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
the Triton MoE backend and the Dynamo 1.6.0 image digest. The worker
and frontend use the existing Python processing and Qwen tool/reasoning parser
combination. There is no speculative decoding or MTP. All consumers stay on
`daedalus-03`; the frontend retains its 4 GiB memory request and 8 GiB limit.

The worker uses one L40S, TP1, the pinned vLLM compilation and CUDA graph
defaults, a 131,072-token model limit,
three sequence slots, prefix caching, chunked prefill and a fixed 4 GiB BF16 KV
pool (`--dtype bfloat16 --kv-cache-dtype auto`). FP8 weight quantization is
unchanged. The installed backend disables runtime KV-scale calibration for
hybrid GDN/Mamba models, and this checkpoint has no calibrated KV-scale tensors
or scheme. BF16 KV avoids relying on the default uncalibrated FP8 scale.

Compilation and graph capture have additional startup and persistent memory
costs outside the fixed KV pool. Qualify the actual capture configuration,
usable context capacity and memory use before workflow acceptance. If graph
startup fails, stop that worker before restoring the captured previous Helm
revision; do not reduce the context, KV pool or reasoning allowance to force a
pass. A previously captured eager worker remains a serving rollback when its
model aliases and client paths match the saved release.

The 4,096-token batch budget accommodates the pinned hybrid GDN/attention
page alignment; a 1,024-token budget cannot satisfy the installed backend's
block-size validation. Three slots preserve the repository's active-actor
concurrency requirement. They do not promise three simultaneous 131,072-token
resident contexts: BF16 full-attention KV alone needs 2.5 GiB per full-length
request, before recurrent state and alignment. Three such contexts exceed the
fixed pool. Compared with FP8 KV, BF16 holds fewer cached tokens in the same
pool; concurrent work can queue or require cache eviction. Actual allocation,
peak memory, eviction behavior and one full context must be qualified after
startup. Usable capacity of at least 131,072 tokens remains a hard load gate.
Do not shorten the context or change the pool to make a failed qualification
appear successful.

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

## Empty-argument tool framing

The post-renderer also mounts a content-addressed, immutable ConfigMap containing
[the frontend launcher](./frontend_tool_framing.py). It checks the installed
XGrammar version and source hash before registering a Qwen grammar wrapper in
the frontend process, then starts the existing Dynamo frontend with its original
arguments. It does not edit installed packages or change the worker image.

For a strict empty-object parameter schema, the wrapper accepts Qwen's native
empty-call closing tag as well as the existing blank-line form. It preserves the
parameter schema and other tool-call constraints. Nonempty schemas, unknown
schema shapes and other model grammars retain their original behavior. This
correction does not change a required tool choice to automatic selection or
insert a dummy tool argument. A runtime/library upgrade requires reviewing and
qualifying the compatibility code again; an unexpected version fails startup.

The October 27 upgrade reviewed this wrapper against XGrammar 0.2.7 in the
pinned Dynamo 1.6.0 image and updated both guards. Automatic, required and named
empty-argument tool choices, streaming tools, and Chat/Responses tool round
trips passed through the LAN gateway and the Daedalus Switchyard application
path. The worker uses the operator's `Recreate` strategy so an upgrade does
not require a second free L40S. See [the stack upgrade notes](../../docs/stack-upgrade-20261027.md).

For a change confined to this launcher, compare the rendered DGD with the
captured release first. Only the frontend command and ConfigMap mount may
change; the worker, ModelExpress, services, routes and PVCs must remain equal.
Such a frontend-only rollout does not require pausing the worker or reloading
weights. Capture the previous Helm revision and restore that frontend if its
startup or required empty-argument tool check fails. Verify that the worker UID
and existing client paths survive both deployment and rollback. Broader model
changes still require the drain procedure below.

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
