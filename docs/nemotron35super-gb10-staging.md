# Private Super staging on GB10

This is an opt-in, text-only staging profile for
`nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4` at revision
`da5b2650a835292c678994a1fa3fb653250fb508`. It uses the pinned Dynamo 1.6
frontend/worker images in the values file. Keep the stack upgrade and existing
deployments described in [the stack notes](stack-upgrade-20261027.md) intact.

The stage is **not a transparent Bolt replacement**: it starts with one sequence,
131072 context, batch 2048, eager execution, an 8 GiB FP8 KV pool, BF16 Mamba,
Marlin experts, no prefix caching or MTP, and no image/video/audio admission.
Model loading, actual usable context, memory peak, protocol behavior, quality
and speed require observation; these settings prove none of them.

The only supported namespace is `daedalus-dynamo`. The stage name is
`nemotron35super-gb10-stage`; its only added endpoint is a private ClusterIP
`nemotron35super-gb10-stage-private:8000`. No gateway route, NodePort, model
catalog entry or existing model alias is changed. **Do not use `make deploy`
for this profile**: that flow reconciles shared gateway/catalog resources.

## Approval boundary

The GB10 is shared with the working Bolt deployment. Super cannot be loaded
beside Bolt under this plan. Pausing Bolt makes generation through the existing
authenticated `daedalus.ddns.me:32359` client path unavailable while Super
occupies the sole GPU, even if its Service, routes, auth and storage are kept.
Obtain direct approval for that interruption and the proposed GB10 use before
pausing or deploying. Preparation commands below do not grant that approval.
Keep every Bolt PVC and model cache; never silently rename Super as Bolt.

## Render and inspect without applying

Run from the repository root with Python/PyYAML and Helm already available:

```bash
umask 077
mkdir -p /tmp/super-stage-review
python3 scripts/prepare_nemotron35super_cache.py \
  > /tmp/super-stage-review/cache.yaml
helm lint charts/dynamo-model \
  --values values/staging/nemotron35super-gb10.yaml
helm template nemotron35super-gb10-stage charts/dynamo-model \
  --namespace daedalus-dynamo \
  --values values/staging/nemotron35super-gb10.yaml \
  --post-renderer ./scripts/nemotron35super_gb10_postrender.py \
  > /tmp/super-stage-review/stage.yaml
```

The post-renderer is mandatory. Raw chart output retains an intentionally
invalid Secret placeholder and the online ModelExpress wait. The adapter
removes both, inserts the offline checker before either Dynamo process, selects
the exact local model/tokenizer snapshot, and adds the private Service. It
accepts only this graph with Frontend/VllmWorker components.

## Cache production and startup checks

The generator emits a separate retained 128 GiB `local-fast` PVC and a CPU-only
Job: no GPU, API token mount, credentials or Secret reference; UID/GID 1000;
6 GiB memory limit; no Job retries; four-hour active deadline. The PVC ownership
annotations match the explicit Helm release above. Review disk headroom,
topology, UID access, image availability and the exact emitted bytes before any
approved creation/download. Do not reuse Bolt's claim.

`runtime/nemotron35super-gb10/cache_integrity.py` is the single source embedded
in the download Job and both startup checkers. The producer invalidates and
fsyncs any old marker before its metadata request. It allows only the exact
public, ungated revision at `https://huggingface.co`, with `token=False`, at most
4096 files and 112 GiB of selected repository bytes. Every file must match its
size and immutable LFS SHA256 or Git blob SHA1. Config/index SHA256 are separately
pinned; the index must name 88 safetensors files totaling 75,010,188,240 bytes.

Only full verification earns `.stage-verified.json`: valid JSON, temporary
file, file fsync, atomic rename and directory fsync. Failure removes apparent
completion. Startup rechecks config/index, every support file's digest,
all recorded file names/sizes and index geometry before importing remote model
code or tokenizers. It rechecks weight sizes, not all 75 GB of weight hashes on
each restart. The marker assumes an exclusively owned cache; it is not an
attestation against an attacker able to rewrite the checker and whole claim.

## Approved staging and rollback sequence

1. Use a clean reviewed source revision. Save private, mode-0600 current Bolt
   DGD, Helm values/manifest and dependent resource identities in a mode-0700
   backup directory. Use explicit `--context kubernetes-admin@kubernetes` for
   `kubectl` and `--kube-context kubernetes-admin@kubernetes` for Helm. Recheck
   the actual release rather than replaying historical values.
2. Under the approved download scope, apply only the reviewed new cache PVC/Job.
   Keep Bolt running if measured CPU/RAM/disk/network headroom allows. Require
   Job completion and the verified marker before scheduling any outage.
3. After direct interruption approval, establish an actual quiet/drained Bolt
   window. Read the current served DGD representation and select the worker by
   component name. The current beta API uses `spec.components[index]`; do not
   replay the chart's alpha `spec.services` path against it. Build a JSON patch
   that tests `metadata.uid` and the **entire freshly saved worker component**,
   then changes only that component's `replicas` to zero at the selected path.
   Prepare the inverse patch with the same UID test and a whole-component test
   against the exact paused component before restoring its original replicas.
   If the operator does not release the worker/GPU, restore its original replica
   count and stop; do not uninstall Bolt or force GPU sharing.
4. Only then install the reviewed stage render with the explicit stage release,
   namespace, values and post-renderer. Set an absolute 20-minute startup bound
   from the actual worker start. Require Ready, no OOM/restarts, expected pinned
   model/backend, and reported usable capacity at least 131072. Record observed
   unified-memory peak; source estimates do not establish fit.
5. Run only separately bounded synthetic text/tool/structured Chat and full-window
   qualification requests for the intended consumer. Check Responses only if an
   intended consumer uses it. A private successful load/protocol test
   does not establish assistant quality or performance benefit, and does not
   authorize changing existing aliases, client routes or production defaults.
6. On failure, stop only the stage, wait until its GPU worker exits, retain its
   cache, and restore the tested Bolt worker component. Use one 15-minute
   recovery observation budget for reconciliation and Ready, not additive waits.
   Restore after qualification unless continued Super GPU occupancy was also
   approved. A late restoration requires continued recovery and reporting, not
   taking another GPU or shrinking the existing model contract.
7. Verify the original Bolt revision/images/context and authenticated client
   paths, including a non-node LAN source and its image/video behavior. Confirm
   Services, auth, routes/catalog, network policy, TLS and PVC identities/specs
   remain intact. Remove only owned temporary Jobs/forwards; preserve all PVCs.
   Record any user-waived check explicitly as waived, never passed; retain an
   existing waiver without asking for the same approval again.

No procedure here guarantees a maximum outage duration. This runbook is a
repeatable staging/rollback contract, not permission for a permanent replacement.
