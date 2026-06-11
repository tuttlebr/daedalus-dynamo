# DynoSim + AIPerf Demo

This opt-in Kubernetes job demonstrates the DynoSim-to-AIPerf workflow for the
Qwen Dynamo deployment:

1. Run offline DynoSim with `python -m dynamo.replay` against the same
   synthetic shared-prefix workload shape used by the live benchmark.
2. Run AIPerf against the in-cluster Dynamo frontend with the same prefix
   ratios, input/output lengths, request count, concurrency, and Dynamo
   `nvext.agent_hints`.
3. Write comparable JSON and CSV summaries into one results PVC.

DynoSim narrows the configuration space before real GPU validation. AIPerf is
still the endpoint-facing validation step, so the two result sets should be
compared directionally rather than treated as identical measurements.

Deploy the demo job into the same namespace as the Dynamo graph:

```bash
kubectl apply -n qwen36-27b -k k8s/benchmarks/dynosim-aiperf
kubectl logs -n qwen36-27b job/dynosim-aiperf-demo -f
```

Results are written to the `dynosim-aiperf-demo-results` PVC under
`/results/dynosim-aiperf-demo`, including:

- `run_config.json`
- `dynosim_summary.json` and `dynosim_summary.csv`
- `aiperf_summary.json` and `aiperf_summary.csv`
- `combined_summary.json` and `combined_summary.csv`
- `decision_summary.json` and `decision_summary.csv`
- `kubernetes/dynamo-graph-deployment-request.yaml`
- `kubernetes/dynamo-graph-deployment-request.json`
- raw DynoSim reports under `dynosim/`
- raw AIPerf artifacts under `aiperf/`

To rerun the job:

```bash
kubectl delete job -n qwen36-27b dynosim-aiperf-demo --ignore-not-found
kubectl apply -n qwen36-27b -k k8s/benchmarks/dynosim-aiperf
```

Default workload:

- endpoint: `http://qwen36-dynamo-fd-ec-frontend:8000`
- prefix ratios: `0.1,0.5,0.9`
- input/output tokens: `4096/128`
- requests/concurrency: `200/4`
- prefix prompt/group count: `8`
- DynoSim workers: `2`
- DynoSim router modes: `round_robin,kv_router`

The job image is the same Qwen Dynamo runtime image used by the deployment. It
must contain a Dynamo version with `python -m dynamo.replay`. If the image only
contains an older Dynamo runtime, the job exits before running AIPerf and tells
you to use a newer Dynamo runtime/source image.

Use `decision_summary.*` first. It normalizes AIPerf's throughput objects into
plain numbers, compares each prefix ratio against the lowest-prefix baseline,
and calls out when the offline DynoSim router modes were indistinguishable.

Use `kubernetes/dynamo-graph-deployment-request.yaml` when you want the
Kubernetes-facing profiler workflow from the Dynamo model deployment guide. The
manifest is a reviewable `DynamoGraphDeploymentRequest` with
`searchStrategy: rapid` and `autoApply: false` by default. After you apply it,
the operator runs AIC-backed DynoSim profiling and stores the generated
`DynamoGraphDeployment` in `.status.profilingResults.selectedConfig`.

```bash
kubectl apply -n qwen36-27b \
  -f /results/dynosim-aiperf-demo/kubernetes/dynamo-graph-deployment-request.yaml
kubectl get dgdr -n qwen36-27b qwen36-dynosim-candidate -w
kubectl get dgdr -n qwen36-27b qwen36-dynosim-candidate \
  -o jsonpath='{.status.profilingResults.selectedConfig}' \
  | python3 -m json.tool > generated-dgd.json
```

## Tuning

Edit the literals in `kustomization.yaml`, then reapply the kustomization.
Useful knobs:

- `RUN_DYNOSIM=false` skips the offline simulation phase.
- `RUN_AIPERF=false` skips the live endpoint validation phase and avoids
  installing AIPerf at startup.
- `PREFIX_RATIOS`, `INPUT_SEQUENCE_LENGTH`, `OUTPUT_SEQUENCE_LENGTH`,
  `REQUEST_COUNT`, `CONCURRENCY`, and `NUM_PREFIX_PROMPTS` define the workload.
- `DYNOSIM_NUM_WORKERS` should match the worker count you want to simulate.
  Offline `kv_router` simulation requires at least two workers, so set
  `DYNOSIM_ROUTER_MODES=round_robin` if you intentionally simulate one worker.
- `DYNOSIM_ENGINE_ARGS_JSON` controls mocker engine settings. The shipped
  default mirrors this repo's SGLang worker shape at the level DynoSim's
  SGLang path accepts: radix prefix caching, chunked prefill with
  `chunked_prefill_size=2048`, `page_size=16`, and `max_running_requests=8`.
- `DYNOSIM_ROUTER_CONFIG_JSON` passes KV-router settings such as
  `router_queue_policy`.
- `DYNOSIM_EXTRA_ARGS` and `AIPERF_EXTRA_ARGS` let you append CLI flags without
  changing the runner script.
- `AIPERF_PIP_SPEC` defaults to `aiperf==0.7.0`; set it to a Git ref if you want
  to test a newer AIPerf build.
- `GENERATE_DGDR_SPEC=true` writes the Kubernetes profiler manifest.
- `DGDR_*` knobs control that manifest, including `DGDR_NAME`, `DGDR_IMAGE`,
  `DGDR_BACKEND`, `DGDR_TTFT_MS`, `DGDR_ITL_MS`, `DGDR_HARDWARE_TOTAL_GPUS`,
  `DGDR_MODEL_CACHE_*`, `DGDR_PLANNER_ENABLED`, and
  `DGDR_OVERRIDES_DGD_JSON`.
- `DGDR_USE_AIPERF_REQUEST_RATE=true` sets `spec.workload.requestRate` from the
  best live AIPerf run. It is off by default because benchmark saturation rate
  is not always the same as your target production load.

## Source Workflow

The DynoSim docs describe `python -m dynamo.replay` as the single-run CLI that
prints an AIPerf-style table and writes a full report JSON. They also recommend
using DynoSim to find promising simulated candidates, then validating the
shortlist on real GPUs with AIPerf:

- <https://github.com/ai-dynamo/dynamo/tree/main/docs/dynosim>
- <https://developer.nvidia.com/blog/dynosim-simulating-the-pareto-frontier>
