# Nemotron 3.5 Bolt on GB10

The image pins vLLM `0.32.0rc1.dev100+g77154ca61`, Dynamo
stable `1.6.0`, ModelExpress `0.7.0`, and b12x `1.5.1`. The serving profile is
[`values/nemotron35bolt-gb10.yaml`](../../values/nemotron35bolt-gb10.yaml).

This is the active model for local inference. The former Qwen IDs are explicit
aliases of Bolt weights, and the Qwen L40S inference worker defaults to zero.
See [single-model serving](../../docs/single-model-serving.md) for compatibility
metadata, guarded rollout and rollback.

## Current serving configuration

The profile disables DFlash and sets `--prefix-match-unit 16` while retaining
prefix caching, the 131,072-token context limit, and the 48 GiB KV pool. This
removes the draft's sliding-window cache group, which disabled fine-grained
hits across the hybrid groups. Without an explicit prefix-match unit, the
remaining groups would default to coarser block-sized prefix hashes. Recheck
the resolved block sizes and actual backend cache hits after an upgrade.

The worker uses image tag
`1.6.0-vllm77154ca-mamba-prefill-20261027-compat1`; the frontend uses
`1.6.0-vllm77154ca-mamba-prefill-20261027`. Both contain stable Dynamo 1.6.0.
Speculation can be restored using the backed-up Helm revision
and values; the existing draft checkpoint remains cached on disk.

Helm revision 7 deployed the cache configuration on October 26; revision 10
upgraded Dynamo on October 27 while retaining these engine settings. The October 26
resolved cache groups were six 4,224-token groups with 16-token prefix matching. A five-turn
replay reused 29,680 of 45,185 prompt tokens (65.69%). An identical repeated
long prompt reused 8,752 of 8,756 tokens, and a constrained reference-number
answer was correct on both cold and warm calls. Exact open-ended reasoning
output was not byte-identical; numerical equivalence is not established by
these functional checks. The full AIPerf workload has not yet been rerun.
See [the deployment report](../../artifacts/bolt-no-dflash-20261026/deployment-report.md).

## Stable Dynamo generation compatibility

The model still requires the pinned vLLM 0.32 development engine. Dynamo 1.6.0
imports an embedding helper whose old ZMQ API was removed in that engine.
[`patch_dynamo_generation_import.py`](./patch_dynamo_generation_import.py)
defers that unused import into the embedding function, allowing generation
startup without substituting an incompatible API. It checks both package
versions and the original source hash; the image build imports the complete
Dynamo vLLM entrypoint afterward. This image supports this Bolt generation
profile; its multiprocess embedding path is not supported.

The October 27 upgrade passed Chat Completions, Responses, streaming tools,
strict empty-argument tool choices, and tool result round trips through the
authenticated LAN gateway. These checks do not replace the previous long-context
or performance qualification. See [the stack upgrade notes](../../docs/stack-upgrade-20261027.md).

## Mamba prefill checkpoint correction

`patch_mamba_prefill_alignment.py` corrects the block size used by
`Scheduler._mamba_block_aligned_split`. The pinned runtime uses the minimum
cache-group block size (`cache_config.block_size`) in that method. DFlash adds
a 64-token group alongside Bolt's 4,496-token Mamba groups. Using 64 to decide
whether a Mamba checkpoint can be exported disagrees with the allocator and
exporter, which use 4,496. The scheduler can therefore skip a required prefill
split even though the exporter cannot produce the checkpoint internally.

The startup log with DFlash confirms ten 4,496-token groups and one 64-token group. These
resolve to a 17,984-token scheduler LCM and 16-token hash granularity. Those
values serve different purposes: the checkpoint's state-slot geometry must
use the Mamba block size. The patch changes that method to read
`cache_config.mamba_block_size`. The pinned model runner already uses the
correct Mamba size when restoring state; it needs no corresponding seed fix.

This is visible with the profile's `--long-prefill-token-threshold 1024`.
For a 9,034-token prompt, the installed method never ends a chunk at its
9,008-token reusable checkpoint and incorrectly skips the split that would
make it available. The allocator's actual checkpoint predicate rejects export
from the final chunk. With the patch, the scheduler ends a chunk at 9,008 and
then processes the remaining 26 tokens, making that checkpoint available.

The October 26 investigation observed zero per-request cached tokens for all
15 diagnostic conversation requests, including preserved-reasoning and
non-thinking variants. A separate identical 8,758-token prompt replay also
reported zero cached tokens. These tests bypassed AIPerf and called the same
Dynamo frontend directly, isolating the failure below the benchmark client.

The Dockerfile applies the correction during image construction. The patch
requires the audited vLLM version and SHA-256 of the original scheduler source
and refuses unexpected upstream code. Rebuild under a new image tag before
selecting the new image in the serving profile. Helm revision 6 originally
deployed image tag `1.6.0dev20261019-vllm77154ca-mamba-prefill-20261026-2`
on October 26. The current stable Dynamo worker retains that scheduler patch.

Run the CPU regression checks with:

```bash
python3 -m unittest discover -s tests -p 'test_bolt_mamba_prefill_alignment.py' -v
```

The fixture contains the installed scheduler method and checkpoint predicates
from the pinned vLLM revision. The tests reproduce missing checkpoints across
all five diagnostic prompt lengths before the correction, verify checkpoint
availability afterward, and preserve behavior when cache-group sizes agree.
The patch was also checked in memory against the running worker's full source,
including its version and source hash, without modifying the worker.

These checks cover checkpoint scheduling with a compatible cache manager.
The previous DFlash configuration used `SlidingWindowManager`, which explicitly disables
fine-grained hits. That forced a 17,984-token reuse alignment, above the default
benchmark prompt lengths. Live repeat tests after the scheduler-only deployment still reported
zero cache hits; this patch alone does not resolve that independent restriction.
A regression test covers the sliding-window fallback explicitly. Those earlier
probes did not produce a cache hit; the subsequent non-speculative deployment
and functional cache checks are described above. Do not assume the modeled
70.23% equals the eventual full-run hit ratio:
chat serialization, block granularity, retained states, and concurrent traffic
still affect it. The investigation does not justify disabling reasoning or
changing the workload to hide the runtime failure.

Detailed local evidence is in
[`artifacts/kv-cache-review-20261026/investigation/`](../../artifacts/kv-cache-review-20261026/investigation/).
