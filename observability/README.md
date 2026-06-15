<!--
SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Observability - Dynamo Unified GPU Dashboard

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
./deploy.sh doctor
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
