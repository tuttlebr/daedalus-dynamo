# Muse Glimmer tool calling and vision deployment

The three Switchyard capability gates failed because Dynamo 1.6.1 lost Muse's
reasoning-to-tool transition during streaming. Buffered tool calls worked, while
streamed calls became ordinary text and ended without `tool_calls`.

## Runtime correction

`runtime/muse-glimmer/` derives from the pinned Dynamo 1.6.1 image with vLLM 0.31.0.
The patch adds `tool_call_start_token = "<atem:function_calls>"` to
`MuseGlimmerToolParser`, which lets Dynamo retain the tool channel during the
reasoning handoff. It deliberately does not add an end marker: early flushing
causes Dynamo's adapter to parse stale text again and duplicate calls when tokens
arrive individually. The patch checks package versions and the original parser's
SHA-256 before changing it.

The image supports both amd64 (GPU worker) and arm64 (existing frontend placement)
and retains the upstream `dynamo` runtime user. Published image:

```text
daedalus.ddns.me:5050/btuttle/daedalus-dynamo-muse-glimmer:1.6.1-vlm-tools-v1
sha256:f2bdf4c7abae9ad7ee19c8c1d2d8a729583557b2da0295aae6db2cc933b9d49a
```

## Vision configuration

The single-L40S tuning file disables `language-model-only` to load the perception
encoder, and enables `enable-multimodal` for Dynamo's request handler. Both are
needed: loading the encoder alone still rejects images with
`Received multimodal data but multimodal processing is not enabled`.

The frontend also sets `DYNAMO_DISABLE_NIXL_MM=1` using the chart's
`frontend.extraEnv`. Dynamo defaults to sending preprocessed image tensors in a
shared-memory segment. Here the frontend runs on `daedalus-06` and the GPU worker
on `daedalus-02`, so that segment is inaccessible to the worker. The frontend
omits the raw image once it has prepared the segment, leaving the worker's
fallback with no image; the model then says that no image was supplied.
Disabling the optional transfer preserves the original image for processing on
the worker. The frontend-only rollout leaves the GPU worker loaded.

DFlash remains enabled with the existing Muse assistant and 15 speculative tokens.
Its drafter uses text inputs; the target Muse model processes the image. Startup
profiling with vision used 28.29 GiB for loaded models, left 9.17 GiB for the KV
cache, and confirmed that the full 131,072-token context fits. These are startup
measurements, not a long-context or throughput benchmark.

The TP1 tuning survives catalog regeneration and does not change TP2 or Spark.
The worker uses zero surge and one unavailable replica so it can restart on its
existing GPU without displacing the other L40S workloads.

## Deployment and validation

Helm release `mx-muse-glimmer-30b-nvfp4-l40s-tp1-84d1be` in namespace
`daedalus-dynamo` was upgraded using the existing recipe and `GATEWAY=0`.
The final release is revision 4, with DGD generation 4 Ready. The frontend and
worker use the published image digest above and have zero restarts.
The latter avoids reconciling shared gateway infrastructure. Model ID, credentials,
LAN ports, ingress routes and network policies are preserved. Only the operator's
internal worker Service changes name as part of its normal rollout.

The original capability gate passed for `hindsight_local`, `local` and `none`
with exactly the settings hashes in the reported failures. Each checked text,
automatic and forced tool round trips, and the reported 131,072-token context.
The installed parser integration test passed 36 cases covering streamed/buffered
responses, automatic/required/named tool choices, one/two calls, and chunk sizes
of one token, 15 tokens and an entire response.

Local validation: 7 recipe-portability tests, 13 Make CLI tests, Helm lint and the
server-side manifest dry run passed. The broad chart-render suite has 18 existing
failures against retired fixtures; the same failures were reproduced against the
unchanged baseline chart. They are unrelated to this patch.

Final live results, collected on 2026-11-01 after revision 4:

| Check | Result |
| --- | --- |
| `hindsight_local` / xhigh | Pass: text, auto tool round trip, forced tool round trip, 131,072 context |
| `local` / high | Pass: text, auto tool round trip, forced tool round trip, 131,072 context |
| `none` / medium | Pass: text, auto tool round trip, forced tool round trip, 131,072 context |
| LAN VIP `192.168.1.12:32360` | Pass: buffered and streamed image recognition; tools at medium/high/xhigh |
| LAN TLS `daedalus.ddns.me:32359` via VIP | Pass: buffered and streamed image recognition; tools at high |
| NodePort `192.168.1.234:32360` | Pass: buffered and streamed image recognition; tools at high |
| Switchyard `192.168.1.234:30260/v1/responses` | Pass: text, auto/forced tool round trips and streamed image recognition for all three local aliases |

All LAN and Switchyard requests ran from `daedalus-07` (`192.168.1.240`), a
non-cluster-node source. The image was a synthetic PNG containing `7429`; the
request asked for the digits without supplying the answer in text. All nine final
image checks returned `7429`. HTTPS preserved hostname/SNI and verified the TLS
certificate, with a process-local DNS override to the LAN VIP. Normal public DNS
for that hostname/port refused connections before this change; DNS and public
port forwarding were not modified.

The final network inventory matches the original client-facing Service, Gateway,
HTTPRoute and policy specs. Live Switchyard configuration is also unchanged.
During rollout, Kubernetes Ready briefly preceded frontend model registration;
transient 404s and missing catalog metadata disappeared once registration finished.
The final results above were collected after registration.

Machine-readable reports are in the artifact directory: `gate-after.jsonl`
(3 passing targets), `lan-after.jsonl` (11 passing checks), and
`switchyard-after.jsonl` (6 passing checks).

## Backup and rollback

Private local evidence and original configuration are under
`artifacts/muse-vlm-20261101T214813Z/`: original Helm values/manifest, original DGD,
network and pod inventory, pre-existing working-tree edits, original recipe files,
build logs and validation outputs. No credentials were recorded in the reports.

To restore the deployment from before this work:

```bash
helm --kube-context kubernetes-admin@kubernetes rollback \
  mx-muse-glimmer-30b-nvfp4-l40s-tp1-84d1be 1 \
  -n daedalus-dynamo --timeout 15m
```

The same command is saved in the artifact directory's `rollback.sh`. Revision 1
restores the original text-only worker and original streaming parser, including
the original tool-call failure. The original local recipe files are separately
backed up in `values-before/`; preserve the user's pre-existing edits if reverting
local files.
