# Dense Qwen3.8 staging on one L40S

The staging overlay restores the exact dense FP8 checkpoint at revision
`017b9c7af6b5689d5dd426a76e0bc077eb5ca20a` to the existing
`qwen38-27b-vllm-l40s` graph. It uses one L40S on `daedalus-03`, a 131,072-token
limit, four sequence slots, a 4,096-token batch budget and a 9 GiB BF16 KV pool.
The FP8 checkpoint has no calibrated KV-scale tensors; BF16 KV avoids relying
on uncalibrated FP8 KV scales. Speculation, MTP, Planner and scaling are disabled.
The vision encoder remains enabled, and image inputs retain the runtime's
existing limits. Both frontend and worker retain a 120-second health request
timeout with health checks enabled, allowing long prefills to finish.
Slots do not guarantee four simultaneous full contexts. Require startup's
reported usable capacity to reach 131,072 and qualify the intended workload.

The post-renderer loads the absolute retained snapshot offline. Its startup
guard checks six metadata hashes and the indexed shard sizes against
`model-pin.json`. These size checks establish completeness, not full weight
checksum verification. The ModelExpress configuration and original cache stay
unchanged. The frontend retains the existing version-pinned empty-argument tool
framing adapter. No gateway, catalog, Switchyard or Bolt route changes belong
to this staging deployment.

Before applying, save the current Helm revision, values and manifest, graph UID
and components, routes, Services, policies and PVC/PV identities. Confirm the
existing worker is zero and only the intended L40S is free. Render and review:

```bash
helm template qwen38-27b-l40s charts/dynamo-model -n daedalus-dynamo \
  -f values/qwen38-27b-l40s.yaml \
  -f values/staging/qwen38-27b-bf16kv-l40s.yaml \
  --post-renderer scripts/qwen38_dense_postrender.py
```

Use the same values and post-renderer with `helm upgrade`, an explicitly verified
Kubernetes context, and a 15-minute startup budget. Do not use the generic
`make deploy` path: it reconciles shared gateway resources. Keep Bolt serving
until the independent quality and promotion gates pass. This overlay is an
explicit staging operation; the shared stack replay retains the retired Qwen
configuration and must not run concurrently with staging.

For rollback, first stop only this graph's new worker with a UID-bound comparison
against the expected component, and wait for its pod to disappear. Restore the
saved Helm revision and verify the previous zero-worker component and frontend.
Retain all cache claims and leave unrelated inference, gateway authentication,
routes and storage untouched. The saved revision is authoritative; do not assume
the historical dense values reproduce the previously active MoE frontend.
