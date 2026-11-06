<!--
SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Observability - Dynamo Dashboards

## Efficiency and SLA patterns

Use the platform-owned `dynamo-worker` PodMonitor for workers. The legacy
`dynamo-decode-worker` monitor overlaps its selectors and must remain absent when
the platform monitor is installed; production model values leave the optional
chart monitor disabled. Otherwise raw worker counter sums double-count requests.

Worker latency/request counters also include Dynamo's internal health canaries.
Use frontend request, TTFT and duration metrics for user-facing latency, and
compare fixed client requests when tuning the engine. Worker counters alone do
not establish user latency during periods with little application traffic.

Import `grafana/dynamo-efficiency-dashboard.json` for the simplified dashboard,
**Dynamo · Efficiency & SLA Patterns** (uid `dynamo-efficiency`). It adds a
separate dashboard and preserves the existing operations dashboard.

Six cards show **average and p95 over the entire selected time range**, per
deployment. Six corresponding charts show rolling averages and p95s. A separate
**Prefix Cache Reuse (vLLM)** card and chart show token-weighted backend reuse.
Defaults
are **24 hours**, a **15-minute trend window**, and a **5-minute refresh** for
intermittent household and autonomous-agent traffic. Select multiple deployments
to compare them; their results remain separate rather than mixing their SLAs.

Serving metrics are fixed to `daedalus-dynamo`. Requests from users, services,
and `daedalus-agent` deployments in `daedalus` are included when they reach these
Dynamo frontends; the client namespace does not filter serving metrics. Client
identity is not available from these metric families.

| Metric | Average | p95 |
| --- | --- | --- |
| Time to First Token | Histogram sum / observation count, in ms | Pooled frontend TTFT histogram buckets |
| Inter-token Latency | Histogram sum / observation count, in ms | Pooled frontend ITL histogram buckets |
| KV Cache Occupancy | Time average of deployment mean worker/rank occupancy | Temporal p95 of that deployment mean |
| Tokens/Watt | Time average of output tokens/s / GPU watts | Temporal p95 of that ratio |
| Input Sequence Length | Histogram sum / observation count, in tokens | Pooled input length histogram buckets |
| Output Sequence Length | Histogram sum / observation count, in tokens | Pooled output length histogram buckets |
| Prefix Cache Reuse (vLLM) | Hit-token increases / queried-token increases | Unavailable from these counters |

Latency and sequence-length summaries use counter increases over the selected
range, weighted by observations. Idle time contributes no requests or tokens
to those populations. p95s are calculated from bucket increases, rather than
averaging previously calculated percentiles. Histograms use their own counts:
TTFT/input length can be observed before a request finishes, whereas output
length is recorded at completion. ITL follows the frontend's token-weighted
stream/chunk observation semantics. Input length includes cached prompt tokens.
The same histogram bucket configuration must be used across the pooled frontend
replicas. Bucket resolution and small observation populations limit p95 accuracy.

**Sample scope** applies to KV occupancy and Tokens/Watt:

- **Active samples** (default) includes only output-producing one-minute rate
  windows, sampled once per minute. This avoids diluting active efficiency and
  cache occupancy with long quiet periods. It is a traffic proxy: it includes
  rate-window padding and excludes prefill-only periods before output begins.
- **All observed time** also includes observed idle windows. This shows the
  effect of idle power on efficiency and idle occupancy on cache utilization.
  It still requires token telemetry; absent telemetry is never filled with zero.

KV occupancy is the equal-weight mean across observed worker DP ranks at each
sample. The `_percent` metric is a fraction and is formatted as a percentage.
This is occupancy, not cache hit rate. Its temporal p95 describes the deployment
mean, rather than the busiest individual rank or a per-request percentile.

**Prefix Cache Reuse (vLLM)** uses `vllm:prefix_cache_hits_total` divided by
`vllm:prefix_cache_queries_total`, with counter increases calculated before
deduplicating scrape jobs and pooling worker engines/ranks within each
deployment. The card uses the entire selected range; the chart uses the trailing
**Trend window**. Historical pod labels retain replaced workers. The ratio is
weighted by queried tokens, not by requests or time-window percentages. It is
independent of **Sample scope** and shows a single reuse ratio; these counters
cannot provide a per-request p95.

Zero hit tokens with positive queried tokens reads **0%**. No queried tokens,
missing counter pairs for an observed engine/rank, and backends without these
vLLM metrics remain **No samples**. This panel measures backend prefix hits,
separately from frontend router estimates and KV capacity occupancy. It includes
all observed client traffic. Select the benchmark's exact time range to compare
with `workload-plan.json`'s modeled reuse estimate; identical token prefixes and
retained cache state are assumptions of that estimate, not guaranteed hits.

Tokens/Watt means **generated output tokens/s divided by GPU board watts**,
equivalent to **output tokens per joule**. It excludes prompt tokens and CPU,
RAM, and wall power. Its average is a mean of sampled ratios, not a total-token /
total-energy accounting integral; its p95 describes high efficiency, not the
least efficient tail. Power is deduplicated by host and physical GPU UUID.
Every live worker must have positive physical GPU reservations with complete
DCGM power mapping at that sample. Unsupported power values, missing worker
mapping, MIG allocations, and boards visibly shared with another pod suppress
the efficiency value. Unreported non-Kubernetes board sharing cannot be detected.

Duplicate scrape jobs are deduplicated before pooling. Historical pod labels
keep replaced pods in selected-range histogram summaries; sampled gauges use
the pod mapping at each historical sample. Missing telemetry remains **No
samples**. An observed histogram with zero increases reads **No observations**.
Trend gaps remain gaps. Scrape/retention gaps and newly appearing counters can
lose observations; one-minute sampling can miss short bursts. The range cards
remain useful across quiet periods without inventing requests or zero latency.

**TTFT objective (ms)** and **ITL objective (ms)** draw editable reference lines
on the latency charts. The initial 1000 ms / 100 ms values are illustrative;
set them to your own objectives. They do not provision alerts or measure SLA
compliance percentages. **Trend window** changes only the charts; the cards
always summarize the dashboard's selected range. Trailing trend windows can
include observations just before the visible range starts.

The simplified dashboard requires Prometheus, kube-state-metrics with Dynamo pod
labels/phase/GPU resource requests, Dynamo frontend and worker metrics, and DCGM
power/GPU telemetry with `exported_pod` / `exported_namespace`. It needs no new
exporters, recording rules, changes to workloads, or network changes. Its
one-minute token-rate window expects scrape intervals of 15 seconds or less
(the current Dynamo scrape interval is 5 seconds).

Import through **Dashboards → New → Import** and upload the JSON. The metrics
source defaults to the Prometheus datasource with uid `prometheus`; select your
datasource in **Metrics source** if necessary. To provision through the existing
Grafana dashboard sidecar instead:

```bash
kubectl create configmap dynamo-efficiency-dashboard \
  --from-file=dynamo-efficiency-dashboard.json=observability/grafana/dynamo-efficiency-dashboard.json \
  -n monitoring --dry-run=client -o yaml \
  | kubectl label --local -f - grafana_dashboard=1 -o yaml \
  | kubectl apply -f -
```

This ConfigMap uses a separate name and dashboard UID. Removing it rolls back
this addition without altering the existing dashboard or serving endpoints.

The JSON is generated from the adjacent Python source. After changing queries
or layout, regenerate and run the focused checks:

```bash
python3 observability/grafana/build-efficiency-dashboard.py
PROMTOOL=/path/to/promtool python3 -m unittest discover \
  -s tests -p 'test_efficiency_dashboard.py' -v
```

`promtool` runs the actual queries against synthetic series covering weighted
averages, pooled p95s, scrape duplication, counter resets, idle versus missing
data, replaced pods, active versus idle-inclusive samples, incomplete/shared
GPU power, and token-weighted prefix reuse with counter resets, duplicate
scrapes, multiple engines/deployments, and incomplete or idle counter pairs.
Those semantic checks skip when `promtool` is unavailable; the JSON
generation consistency check still runs. No live cluster is needed for them.

Metric references: [Dynamo metrics catalog](https://docs.nvidia.com/dynamo/dev/reference/observability/metrics-catalog)
and [Prometheus query functions](https://prometheus.io/docs/prometheus/latest/querying/functions/).

## Unified GPU operations dashboard

`grafana/dynamo-gpu-demo-dashboard.json` (uid `dynamo-gpu-demo`) is the shared
Grafana dashboard for the Dynamo examples in this repository. It covers vLLM,
SGLang, and TensorRT-LLM deployments with one set of namespace/deployment
variables and engine-specific PromQL targets where the backend metric names
diverge.

The dashboard combines inference golden signals, Dynamo serving internals, KV
router metrics, KV cache reuse and utilization, engine saturation, GPU telemetry
correlated to worker pods, and Kubernetes/node health.

## Prerequisites

The dashboard expects an existing Kubernetes monitoring stack:

| Component | Provides | Notes |
| --- | --- | --- |
| Prometheus | scrape + storage | Grafana datasource uid must be `prometheus`. |
| kube-state-metrics | `kube_pod_*`, `kube_node_*` | Pod discovery assumes Grove `created_by_kind=PodClique`. |
| node-exporter | `node_*`, `machine_cpu_cores`, `container_*` | Bundled with kube-prometheus-stack. |
| NVIDIA DCGM exporter | `DCGM_FI_DEV_*` | Must run with Kubernetes pod mapping for `exported_pod` / `exported_namespace`. |
| Dynamo metrics scraping | `dynamo_frontend_*`, `dynamo_component_*`, engine metrics | Scrape the frontend and worker metrics endpoints. |

Engine-specific panels use these metric families when present:

- vLLM: `vllm:*`
- SGLang: `sglang:*`
- TensorRT-LLM: `trtllm_*`

TensorRT-LLM panels include KV cache hit rate/utilization, request queue/load,
prompt/generation token, and prefill/decode histogram metrics. If TensorRT-LLM is
running with the PyTorch backend, iteration statistics may require
`enable_iter_perf_stats: true`.

## Interconnect (optional)

NIXL/NCCL/RDMA fabric panels are a future addition and are relevant only when
the deployment runs under `TRANSPORT_PROFILE=rdma` or `SERVING_MODE=disagg`. The
current dashboard does not include them. To validate the interconnect fabric
today, use the cluster doctor:

```bash
make doctor
```

## GPU Worker Correlation

The GPU "by Dynamo Worker" panels and scoped overview power/util tiles rely on
DCGM exporter attaching `exported_pod` / `exported_namespace` labels. Plain
`pod` / `namespace` labels on DCGM series normally identify the DCGM daemonset,
not the inference worker using the GPU.

## Install

Import in Grafana:

1. Dashboards -> New -> Import.
2. Upload `observability/grafana/dynamo-gpu-demo-dashboard.json`.
3. Select the Prometheus datasource with uid `prometheus`.

Provision with the kube-prometheus-stack Grafana sidecar:

```bash
kubectl create configmap dynamo-gpu-demo-dashboard \
  --from-file=dynamo-gpu-demo-dashboard.json=observability/grafana/dynamo-gpu-demo-dashboard.json \
  -n monitoring --dry-run=client -o yaml \
  | kubectl label --local -f - grafana_dashboard=1 -o yaml \
  | kubectl apply -f -
```

## Use

Set the Namespace and Deployment variables to scope the Dynamo panels. The GPU
Host, GPU Model, and GPU variables scope only the cluster-wide GPU Telemetry row.

The dashboard recognizes common aggregated and disaggregated worker role names,
including `vllmworker`, `sglangworker`, `trtllmworker`,
`tensorrtllmworker`, `prefill`, `decode`, `backend`, `worker`, `encoder`, and
`encode`.

## Gateway FD Metrics

The inference gateway includes a same-UID metrics sidecar that reads the proxy
process through the shared pod process namespace. It exports:

- `daedalus_gateway_process_open_fds`
- `daedalus_gateway_process_soft_limit_fds`
- `daedalus_gateway_process_hard_limit_fds`
- `daedalus_gateway_process_fd_headroom_fds`
- `daedalus_gateway_process_fd_utilization_ratio`
- `daedalus_gateway_pod_tcp_sockets{state=...}`

`make gateway-monitoring` creates a `ServiceMonitor` and a `PrometheusRule` when
the Prometheus Operator CRDs are installed. The rules warn at 80 percent FD
utilization, become critical at 90 percent, detect missing FD telemetry, and
alert when available gateway replicas fall below two.

Inspect one pod directly through the Kubernetes API:

```bash
pod=$(kubectl get pod -n daedalus-dynamo \
  -l gateway.networking.k8s.io/gateway-name=inference-gateway \
  -o jsonpath='{.items[0].metadata.name}')
kubectl get --raw \
  "/api/v1/namespaces/daedalus-dynamo/pods/${pod}:9090/proxy/metrics"
```
