# Pinned official FP8 cache preparation

This prepares a separate cache for
`Qwen/Qwen3.6-35B-A3B-FP8@95a723d08a9490559dae23d0cff1d9466213d989`.
It does not start a model or alter any serving workload, alias, endpoint,
credential, GPU allocation or existing PVC. Keep the old model and Redis claims.
The new retained 64 GiB RWX claim uses `nfs-client` and must be accessible to
UID/GID 1000. Check available storage and the claim's actual mount permissions
before downloading; API admission alone does not prove access.

From a clean reviewed repository revision, render a uniquely named CPU Job:

```sh
python3 scripts/prepare_qwen36_fp8_cache.py render-job \
  --job-name qwen36-fp8-cache-95a723d0-001 \
  > /tmp/qwen36-fp8-cache-job.yaml
```

Rendering is the default preparation step; it does not contact Kubernetes or
download files. After reviewing the rendered ConfigMap and Job, the operator
can create only the new claim and these preparation resources under the
verified cluster context:

```sh
kubectl --context "$EXPECTED_CONTEXT" apply \
  -f runtime/qwen36-35b-fp8-l40s/cache-pvc.yaml
kubectl --context "$EXPECTED_CONTEXT" create -f /tmp/qwen36-fp8-cache-job.yaml
kubectl --context "$EXPECTED_CONTEXT" -n daedalus-dynamo \
  wait --for=condition=Complete job/qwen36-fp8-cache-95a723d0-001 --timeout=7200s
```

The Job is pinned to node03, uses the immutable existing Dynamo 1.4.1 image with
`IfNotPresent`, requests no GPU, and explicitly hides NVIDIA/CUDA devices. It
does not mount a Kubernetes service-account token. Its optional HF token comes
from the existing `hf-token-secret`; no credential value appears in arguments
or manifests. It has no automatic retry. For a retry, inspect the failure and
render a new unique Job name; the pinned Hub cache can resume partial files.
Preserve the PVC and marker. Do not run preparation against an actively serving
candidate cache.

The command inside the Job is `hf download` with the full pinned revision and
two download workers. Verification resolves Hub metadata for that same immutable
revision, checks the six pinned model/tokenizer/template/generation metadata
SHA-256 values, all 42 shard sizes and LFS SHA-256 hashes, and remaining metadata
against its Hub LFS hashes or Git blob identities. Snapshot symlinks may resolve only inside
the new cache. A previous marker is removed before preparation; only successful
complete verification atomically publishes `/model/fp8-95a723d0-verified.json`.
Failure leaves no new success marker. CLI errors are bounded and token values
are not printed.

The marker proves cache identity and integrity, not usable GPU capacity,
tokenizer behavior, speed or briefing quality. Serving qualification must still
verify the exact tokenizer/template controls, at least 131072 usable context,
three active sequences, actual loaded/peak memory, old request-alias behavior
and the complete application protocol. No context clipping or GPU-sharing
workaround follows from a successful download. A serving transition and rollback
are separate operator actions and require draining current consumers first.

The initial serving pool is 4 GiB BF16 KV; weight quantization remains FP8.
Three scheduling slots do not promise three simultaneously resident full-length
131072-token inputs: their raw attention KV alone needs 7.5 GiB, before
recurrent state, alignment and other overhead. BF16 holds fewer cache tokens
than FP8 in the same pool, so work can queue or require cache eviction. Actual
capacity and peak memory remain unproven until startup; usable capacity of at
least 131072 tokens is a hard load gate. Keep the full window and existing pool
while measuring that fit. This serving precision choice does not change cache
downloads, weight files or the integrity marker.
