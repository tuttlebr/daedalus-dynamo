<!--
SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Observability — Dynamo GPU Demo Dashboard

`grafana/dynamo-gpu-demo-dashboard.json` (uid **`dynamo-gpu-demo`**) is a demo-oriented
Grafana dashboard for monitoring this Dynamo deployment on Kubernetes. It combines
inference golden signals, **Dynamo serving internals** (KV router, batching/saturation,
KV-cache occupancy, prefill/decode), error rate, GPU telemetry **correlated to the worker
pods**, and Kubernetes/node health.

The repo ships only the dashboard JSON; it expects an existing monitoring stack. This file
documents what that stack must provide.

## Prerequisites

The dashboard is built and verified against the Daedalus monitoring stack:

| Component | Provides | Notes |
| --- | --- | --- |
| **Prometheus** (kube-prometheus-stack, ns `monitoring`) | scrape + storage | Grafana datasource **uid must be `prometheus`** (see below). |
| **kube-state-metrics** | `kube_pod_*`, `kube_node_*` | Bundled with kube-prometheus-stack. Pod panels assume Grove `created_by_kind=PodClique`. |
| **node-exporter** | `node_*`, `machine_cpu_cores`, `container_*` | Bundled with kube-prometheus-stack. |
| **NVIDIA DCGM exporter** (gpu-operator, ns `gpu-operator`) | `DCGM_FI_DEV_*` | **Must run with Kubernetes pod-mapping** so GPU series carry `exported_pod` / `exported_namespace`. |
| **Dynamo metrics scraping** (Dynamo operator, ns `dynamo-system`) | `dynamo_frontend_*`, `dynamo_component_*`, `sglang:*` | ServiceMonitors/PodMonitors create jobs `dynamo-system/dynamo-frontend` (:8000) and `dynamo-system/dynamo-worker` (:9090). The SGLang worker exports `sglang:*` engine metrics on its system port only when started with `--enable-metrics`. |

### GPU ↔ worker correlation (important)

The GPU "by Dynamo Worker" panels and the scoped overview power/util tiles rely on the
DCGM exporter attaching **`exported_pod` / `exported_namespace`** labels (the gpu-operator
`kubernetes-process-mapping` feature). Note the gotcha: DCGM series also carry plain
`pod` / `namespace` labels, but those point at the **DCGM daemonset itself** — the
dashboard deliberately uses `exported_*` to map a GPU to the Dynamo worker using it.
The cluster-wide GPU Telemetry panels use `Hostname` / `gpu` / `modelName` and work without
pod-mapping.

### Datasource

Every panel references a Prometheus datasource with **uid `prometheus`**. If your Grafana
provisions Prometheus under a different uid, either:

- rename/alias the datasource uid to `prometheus`, or
- find-and-replace `"uid": "prometheus"` in the JSON before importing, or
- (for sharing across instances) convert the datasource to a dashboard variable
  (`templating` entry of type `datasource`) and re-point every panel at `${datasource}`.

## Install the dashboard

**Import (quickest):** Grafana → Dashboards → **New → Import** → upload
`grafana/dynamo-gpu-demo-dashboard.json` → select your Prometheus datasource.

**Provision (kube-prometheus-stack Grafana sidecar):** create a labeled ConfigMap; the
Grafana sidecar auto-loads it.

```bash
kubectl create configmap dynamo-gpu-demo-dashboard \
  --from-file=dynamo-gpu-demo-dashboard.json=grafana/dynamo-gpu-demo-dashboard.json \
  -n monitoring --dry-run=client -o yaml \
  | kubectl label --local -f - grafana_dashboard=1 -o yaml \
  | kubectl apply -f -
```

(Optional: add the annotation `k8s-sidecar-target-directory` or a `grafana_folder` label to
place it in a folder.)

## Use it

1. Set the **Namespace** (`qwen36-27b`) and **Deployment** (`qwen36-dynamo-fd-ec`)
   variables — they scope every Dynamo/GPU panel. The **GPU Host / Model / GPU** variables
   scope the cluster-wide GPU Telemetry row.
2. Read top-to-bottom: **Overview** (golden signals + live saturation) → **Model & Engine
   Configuration** → **Workload & Latency** → **Dynamo Serving Internals** → **GPU
   Telemetry** → **Kubernetes & Node Health**.
3. The dashboard is intentionally comprehensive (6 rows). Collapse rows you do not need;
   default refresh is 10s.

## Drive load for the demo

The serving-internals, KV-cache, saturation, and error panels are most
compelling under load. Two easy generators:

```bash
# Synthetic shared-prefix router benchmark (sweeps prefix ratios + priorities)
kubectl apply -n qwen36-27b -k ../k8s/benchmarks/aiperf
kubectl logs -n qwen36-27b job/aiperf-router-demo -f

# Or the companion Photo Tagger app (drives real multimodal/image traffic)
#   UI on NodePort 30152 — see ../README.md
```

What to watch as load ramps:

- **In-flight vs Waiting (per worker)** climbing toward the dashed line at `max-running-requests=8`.
- **KV-Cache Utilization** rising; **Queue Wait p95 & Retractions** spiking if oversubscribed.
- **KV Router Hit Rate** and **Radix-Cache Hit Rate** reflecting shared prefixes.
- **Image (multimodal) traffic** registering on throughput/latency panels when the Photo
  Tagger runs (SGLang serves a vision-language model but exposes no separate
  multimodal-cache metric, so that panel is SGLang-specific and may read empty).
- **GPU Utilization / Memory-Bandwidth Util by Dynamo Worker** tracking the workers' GPUs.

## Notes

- Architecture is **aggregated** (prefill+decode on the same workers) with KV-aware routing;
  the serving-internals panels reflect that. If you move to disaggregated serving, the
  router/per-worker panels already split by `worker_type` (prefill/decode).
- DCGM here exposes the default field set (util, memory, power, temp, clock, mem-bandwidth).
  Tensor-core / NVLink panels would require enabling DCGM profiling (DCP) fields on the
  exporter (`DCGM_FI_PROF_*`), which are not collected by default.
