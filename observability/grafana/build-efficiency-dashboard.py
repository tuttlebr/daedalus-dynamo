#!/usr/bin/env python3
"""Render the standalone Dynamo efficiency dashboard (standard library only)."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path


DATASOURCE = {"type": "prometheus", "uid": "${datasource}"}
NAMESPACE = 'namespace="daedalus-dynamo"'
GRAPH_LABEL = "label_nvidia_com_dynamo_graph_deployment_name"
DEPLOYMENTS = f'{GRAPH_LABEL}=~"${{deployment:regex}}"'
STEP = "1m"


def pod_map(window: str | None = None, workers: bool = False) -> str:
    selector = f"{NAMESPACE},{DEPLOYMENTS}"
    if workers:
        selector += ',label_nvidia_com_dynamo_component_type=~"worker|prefill|decode"'
    labels = f"kube_pod_labels{{{selector}}}"
    if window:
        labels = f"max_over_time({labels}[{window}])"
    return (
        f'label_replace(max by (namespace, pod, {GRAPH_LABEL}) ({labels}), '
        f'"deployment", "$1", "{GRAPH_LABEL}", "(.+)")'
    )


def scoped(expression: str, mapping: str) -> str:
    return f"({expression}) * on (namespace, pod) group_left (deployment) ({mapping})"


def frontend(metric: str, window: str, bucket: bool = False) -> str:
    # Deduplicate scrape jobs only AFTER calculating each counter's increase.
    dimensions = "namespace, pod, model" + (", le" if bucket else "")
    values = f"max by ({dimensions}) (increase({metric}{{{NAMESPACE}}}[{window}]))"
    grouping = "deployment, le" if bucket else "deployment"
    return f"sum by ({grouping}) ({scoped(values, pod_map(window))})"


def histogram(metric: str, window: str, factor: int = 1, idle: bool = False) -> list[str]:
    count = frontend(metric + "_count", window)
    total = frontend(metric + "_sum", window)
    buckets = frontend(metric + "_bucket", window, bucket=True)
    expressions = [
        f"({factor} * ({total}) / ({count})) and on (deployment) (({count}) > 0)",
        f"({factor} * histogram_quantile(0.95, {buckets})) and on (deployment) (({count}) > 0)",
    ]
    if idle:
        # An observed, unchanged histogram is idle; an absent histogram stays N/A.
        expressions = [
            f"({expression}) or on (deployment) (-1 + 0 * (({count}) == 0))"
            for expression in expressions
        ]
    return expressions


def prefix_reuse(window: str) -> str:
    """Token-weighted vLLM backend hits, with complete counter-pair coverage."""
    dimensions = "namespace, pod, model_name, engine, dp_rank"
    mapping = pod_map(window, workers=True)

    def counter(metric: str) -> str:
        # Calculate resets/increases per scrape before deduplicating jobs.
        return scoped(
            f"max by ({dimensions}) (increase({metric}{{{NAMESPACE}}}[{window}]))",
            mapping,
        )

    hits = counter("vllm:prefix_cache_hits_total")
    queries = counter("vllm:prefix_cache_queries_total")
    total_hits = f"sum by (deployment) ({hits})"
    total_queries = f"sum by (deployment) ({queries})"
    # A partially missing numerator must not look like a lower hit ratio.
    match = f"on (deployment, {dimensions})"
    incomplete = f"(({queries}) unless {match} ({hits})) or (({hits}) unless {match} ({queries}))"
    return (
        f"((({total_hits}) / ({total_queries})) and on (deployment) (({total_queries}) > 0)) "
        f"unless on (deployment) ({incomplete})"
    )


def samples() -> tuple[str, str, str]:
    mapping = pod_map()
    live_workers = (
        f"({pod_map(workers=True)}) and on (namespace, pod) "
        'max by (namespace, pod) (kube_pod_status_phase{phase=~"Pending|Running|Unknown"} == 1)'
    )
    output = scoped(
        f"max by (namespace, pod, model) (rate(dynamo_frontend_output_tokens_total{{{NAMESPACE}}}[{STEP}]))",
        mapping,
    )
    output = f"sum by (deployment) ({output})"
    # Both modes require observed token telemetry. Zero is valid only in All time.
    activity = f"(({output}) > 0) or (({output}) and on () (vector($active_only) == 0))"
    kv = scoped(
        f"max by (namespace, pod, dp_rank) (dynamo_component_gpu_cache_usage_percent{{{NAMESPACE}}})",
        live_workers,
    )
    # Dynamo reports this gauge as a fraction despite its _percent suffix.
    kv = f"(avg by (deployment) ({kv})) and on (deployment) ({activity})"

    # Full-board power is attributed using the worker labels supplied by DCGM.
    # Exclude MIG, unsupported sentinel values, and visibly shared boards.
    board_labels = "Hostname, UUID"
    raw_power = (
        'DCGM_FI_DEV_POWER_USAGE{exported_namespace="daedalus-dynamo",'
        'exported_pod!="",UUID!="",GPU_I_ID=""}'
    )
    exclusive = (
        f"count by ({board_labels}) (max by ({board_labels}, exported_namespace, exported_pod) "
        '(DCGM_FI_DEV_GPU_UTIL{exported_pod!="",UUID!=""})) == 1'
    )
    valid_power = (
        f"(({raw_power} > 0) < 1000000) and on ({board_labels}) ({exclusive})"
    )
    mapped_power = (
        f'label_replace(label_replace(({valid_power}), "namespace", "$1", '
        '"exported_namespace", "(.+)"), "pod", "$1", "exported_pod", "(.+)")'
    )
    mapped_power = scoped(mapped_power, live_workers)
    power = f"sum by (deployment) (max by (deployment, {board_labels}) ({mapped_power}))"

    # Every live worker must have all its requested physical GPUs reporting power.
    # Init containers and regular containers use Kubernetes' max(init, sum(app)).
    regular = (
        "sum by (namespace, pod) (max by (namespace, pod, container) "
        f'(kube_pod_container_resource_requests{{{NAMESPACE},resource="nvidia_com_gpu"}}))'
    )
    init = (
        "max by (namespace, pod) "
        f'(kube_pod_init_container_resource_requests{{{NAMESPACE},resource="nvidia_com_gpu"}})'
    )
    requests = (
        'max by (namespace, pod) ('
        f'label_replace(({regular}), "request_class", "app", "pod", ".*") or '
        f'label_replace(({init}), "request_class", "init", "pod", ".*"))'
    )
    requests = scoped(f"({requests}) > 0", live_workers)
    mapped_count = (
        "count by (deployment, namespace, pod) "
        f"(max by (deployment, namespace, pod, {board_labels}) ({mapped_power}))"
    )
    covered_workers = f"({requests}) == on (deployment, namespace, pod) ({mapped_count})"
    complete = (
        f"count by (deployment) ({live_workers}) == on (deployment) "
        f"count by (deployment) ({covered_workers})"
    )
    mig = scoped(
        f'kube_pod_container_resource_requests{{{NAMESPACE},resource=~"nvidia_com_mig_.*"}} > 0',
        live_workers,
    )
    efficiency = (
        f"((({output}) / ({power})) and on (deployment) ({complete})) "
        f"unless on (deployment) (count by (deployment) ({mig}))"
    )
    efficiency = f"({efficiency}) and on (deployment) ({activity})"
    return kv, efficiency, activity


def sampled_summary(expression: str, window: str) -> list[str]:
    return [
        f"avg_over_time(({expression})[{window}:{STEP}])",
        f"quantile_over_time(0.95, ({expression})[{window}:{STEP}])",
    ]


def target(expression: str, ref: str, legend: str, instant: bool) -> dict:
    return {
        "datasource": DATASOURCE,
        "editorMode": "code",
        "expr": expression,
        "refId": ref,
        "legendFormat": legend,
        "instant": instant,
        "range": not instant,
        "format": "time_series",
    }


def panel(panel_id: int, title: str, description: str, unit: str,
          expressions: list[str], x: int, y: int, instant: bool,
          labels: tuple[str, ...] = ("Average", "p95")) -> dict:
    defaults = {
        "unit": unit,
        "decimals": 1,
        "min": 0,
        "noValue": "No samples",
        "color": {"mode": "palette-classic"},
        "mappings": [{"type": "value", "options": {
            "-1": {"text": "No observations", "color": "text", "index": 0}
        }}] if instant else [],
    }
    if unit == "percentunit":
        defaults["max"] = 1
    result = {
        "id": panel_id,
        "type": "stat" if instant else "timeseries",
        "title": title,
        "description": description,
        "datasource": DATASOURCE,
        "gridPos": {"h": 5 if instant else 8, "w": 8, "x": x, "y": y},
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "targets": [target(expr, ref, "{{deployment}} · " + label, instant)
                    for expr, ref, label in zip(expressions, ["A", "B"], labels)],
    }
    if instant:
        result["options"] = {
            "reduceOptions": {"values": False, "calcs": ["lastNotNull"], "fields": ""},
            "orientation": "horizontal", "textMode": "value_and_name",
            "colorMode": "none", "graphMode": "none", "justifyMode": "auto",
            "wideLayout": False, "text": {"valueSize": 30, "titleSize": 12},
        }
    else:
        result.update({"interval": STEP, "maxDataPoints": 1440})
        defaults["custom"] = {
            "drawStyle": "line", "lineInterpolation": "stepAfter", "lineWidth": 2,
            "fillOpacity": 8, "showPoints": "auto", "pointSize": 4,
            "spanNulls": False, "insertNulls": False, "axisCenteredZero": False,
            "axisColorMode": "text", "axisLabel": "", "axisPlacement": "auto",
            "barAlignment": 0, "gradientMode": "none", "hideFrom": {
                "legend": False, "tooltip": False, "viz": False,
            }, "scaleDistribution": {"type": "linear"},
            "stacking": {"group": "A", "mode": "none"},
            "thresholdsStyle": {"mode": "off"},
        }
        result["fieldConfig"]["overrides"] = [{
            "matcher": {"id": "byRegexp", "options": ".* · p95$"},
            "properties": [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [5, 4]}}],
        }]
        result["options"] = {
            "legend": {"displayMode": "list", "placement": "bottom", "calcs": []},
            "tooltip": {"mode": "multi", "sort": "desc", "hideZeros": False},
        }
    return result


def build_dashboard() -> dict:
    kv, efficiency, _ = samples()
    metrics = [
        ("Time to First Token", "ms", "dynamo_frontend_time_to_first_token_seconds", 1000,
         "Request-weighted frontend TTFT. p95 is computed from pooled histogram buckets, "
         "never by averaging worker or time-window percentiles. Idle time adds no observations."),
        ("Inter-token Latency", "ms", "dynamo_frontend_inter_token_latency_seconds", 1000,
         "Frontend token-weighted inter-token delivery latency, including streaming/chunk delivery effects. "
         "Average and p95 use the ITL histogram's own observation population."),
        ("KV Cache Occupancy", "percentunit", kv, None,
         "At each one-minute sample, average the KV occupancy fractions across selected worker DP ranks "
         "within each deployment. Average and p95 then describe that deployment mean over time. "
         "This is cache occupancy, not cache hit rate; unequal rank capacities are not weighted. "
         "Sample scope controls whether quiet periods are included."),
        ("Tokens/Watt", "suffix: tok/s/W", efficiency, None,
         "Generated output tokens/s divided by summed physical GPU board watts, equivalent to output tokens/J. "
         "Average and p95 are the mean and temporal percentile of one-minute sampled ratios. "
         "Prompt tokens are excluded. Every live worker must have complete physical-GPU power mapping. "
         "MIG and visibly shared boards are excluded. GPU power excludes CPU, RAM, and wall power. "
         "Sample scope controls whether idle GPU power is reflected."),
        ("Input Sequence Length", "suffix: tokens", "dynamo_frontend_input_sequence_tokens", 1,
         "Request-weighted prompt length observed at first output token. Includes cached prompt tokens. "
         "Average and p95 come from the input sequence histogram."),
        ("Output Sequence Length", "suffix: tokens", "dynamo_frontend_output_sequence_tokens", 1,
         "Request-weighted generated sequence length observed at completion. "
         "Ongoing streams do not enter this population until the frontend records the observation."),
    ]
    panels = [{
        "id": 1, "type": "text", "title": "Dynamo efficiency & latency",
        "gridPos": {"h": 3, "w": 24, "x": 0, "y": 0},
        "options": {"mode": "markdown", "content": (
            "**The cards summarize the entire selected time range, per deployment.** "
            "Start with **24 hours** or **7 days** for occasional household and agent traffic. "
            "Below, **Trend window** controls smoothing; solid lines show averages and dashed lines p95.\n\n"
            "**KV Cache Occupancy** measures capacity in use. **Prefix Cache Reuse (vLLM)** below measures "
            "cached / queried prompt tokens; compare that ratio with benchmark reuse estimates. "
            "**Sample scope** applies only to occupancy and Tokens/Watt: active samples are output-producing one-minute windows; "
            "choose **All observed time** to include quiet periods. Latency and length always use actual observations. "
            "Gaps stay gaps; missing telemetry stays unavailable. Serving metrics are scoped to `daedalus-dynamo`, "
            "including requests from clients and agents in `daedalus`."
        )},
    }]
    for index, (name, unit, metric, factor, description) in enumerate(metrics):
        if factor is None:
            summary = sampled_summary(metric, "$__range")
            trend = sampled_summary(metric, "$trend_window")
        else:
            summary = histogram(metric, "$__range", factor, idle=True)
            trend = histogram(metric, "$trend_window", factor)
        summary_panel = panel(10 + index, name + " · selected range", description,
                              unit, summary, (index % 3) * 8, 3 + (index // 3) * 5, True)
        trend_panel = panel(20 + index, name + " · $trend_window trend", description,
                            unit, trend, (index % 3) * 8, 14 + (index // 3) * 8, False)
        if index in (0, 1):
            objective = "ttft_objective_ms" if index == 0 else "itl_objective_ms"
            trend_panel["targets"].append(target(f"vector(${objective})", "C", "Objective", False))
            trend_panel["fieldConfig"]["overrides"].append({
                "matcher": {"id": "byName", "options": "Objective"},
                "properties": [
                    {"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}},
                    {"id": "custom.fillOpacity", "value": 0},
                    {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [2, 5]}},
                ],
            })
        panels.extend([summary_panel, trend_panel])
    panels.append({"id": 2, "type": "row", "title": "Latency, utilization & workload patterns",
                   "collapsed": False, "panels": [],
                   "gridPos": {"h": 1, "w": 24, "x": 0, "y": 13}})
    reuse_description = (
        "vLLM backend prefix-cache hit tokens divided by queried tokens, using counter increases over the "
        "selected range (card) or trailing Trend window (chart). Pools tokens across worker engines/ranks "
        "within each deployment after deduplicating scrapes; historical pod labels retain replaced workers. "
        "This is token-weighted reuse, not KV capacity occupancy or a frontend router estimate. "
        "No per-request p95 is available from these counters. Sample scope does not apply. "
        "Zero hits with observed queried tokens is 0%; no queries, missing counter pairs, "
        "and backends without these vLLM metrics remain No samples. Includes all observed client traffic. "
        "Select the benchmark's exact time range to compare with its modeled reuse estimate; "
        "the estimate assumes identical token prefixes and retained cache state, not guaranteed hits."
    )
    panels.append({"id": 3, "type": "row", "title": "Prefix cache reuse · vLLM backend",
                   "collapsed": False, "panels": [],
                   "gridPos": {"h": 1, "w": 24, "x": 0, "y": 30}})
    reuse_card = panel(16, "Prefix Cache Reuse (vLLM) · selected range", reuse_description,
                       "percentunit", [prefix_reuse("$__range")], 0, 31, True, labels=("Reuse",))
    reuse_card["gridPos"]["h"] = 8
    reuse_trend = panel(26, "Prefix Cache Reuse (vLLM) · $trend_window trend", reuse_description,
                        "percentunit", [prefix_reuse("$trend_window")], 8, 31, False, labels=("Reuse",))
    reuse_trend["gridPos"]["w"] = 16
    panels.extend([reuse_card, reuse_trend])
    panels.sort(key=lambda p: (p["gridPos"]["y"], p["gridPos"]["x"]))
    deployment_query = (
        f'query_result(count by ({GRAPH_LABEL}) '
        f'(max_over_time(kube_pod_labels{{{NAMESPACE},{GRAPH_LABEL}!=""}}[$__range])))'
    )
    return deepcopy({
        "id": None, "uid": "dynamo-efficiency", "title": "Dynamo · Efficiency & SLA Patterns",
        "description": "Six selected-range average/p95 summaries and rolling trends, plus token-weighted "
                       "vLLM prefix-cache reuse, for Dynamo deployments in daedalus-dynamo. "
                       "Active-sample KV occupancy and output-token efficiency "
                       "separate useful work from expected idle time; choose All observed time to include idle.",
        "editable": True, "schemaVersion": 42, "version": 2, "timezone": "browser",
        "refresh": "5m", "time": {"from": "now-24h", "to": "now"},
        "timepicker": {"refresh_intervals": ["1m", "5m", "15m", "30m", "1h"]},
        "tags": ["dynamo", "daedalus", "efficiency", "sla"], "graphTooltip": 1,
        "annotations": {"list": []},
        "links": [{"title": "Metric definitions", "type": "link", "targetBlank": True,
                   "url": "https://docs.nvidia.com/dynamo/dev/reference/observability/metrics-catalog"}],
        "templating": {"list": [
            {"name": "datasource", "label": "Metrics source", "type": "datasource",
             "query": "prometheus", "current": {"text": "Prometheus", "value": "prometheus"},
             "options": [], "refresh": 1},
            {"name": "deployment", "label": "Deployment", "type": "query", "datasource": DATASOURCE,
             "query": {"query": deployment_query, "refId": "StandardVariableQuery"},
             "definition": deployment_query, "regex": f'/{GRAPH_LABEL}="([^"]+)"/',
             "refresh": 2, "sort": 1, "multi": True, "includeAll": True, "allValue": ".+",
             "current": {"text": "All", "value": "$__all"}, "options": [],
             "description": "Includes deployments observed anywhere in the selected range, including replaced pods."},
            {"name": "trend_window", "label": "Trend window", "type": "custom",
             "query": "5m,15m,30m,1h,6h", "current": {"text": "15m", "value": "15m"},
             "options": [{"text": value, "value": value, "selected": value == "15m"}
                         for value in ["5m", "15m", "30m", "1h", "6h"]],
             "description": "Trailing trend window; selected-range cards always use the dashboard time range."},
            {"name": "active_only", "label": "Sample scope", "type": "custom",
             "query": "Active samples : 1,All observed time : 0",
             "current": {"text": "Active samples", "value": "1"},
             "options": [{"text": "Active samples", "value": "1", "selected": True},
                         {"text": "All observed time", "value": "0", "selected": False}],
             "description": "Occupancy and efficiency only; prefix reuse always uses observed tokens. "
                            "Active = output-producing one-minute windows, including rate-window padding."},
            {"name": "ttft_objective_ms", "label": "TTFT objective (ms)", "type": "textbox",
             "query": "1000", "current": {"text": "1000", "value": "1000"}, "options": [],
             "description": "Illustrative reference line; enter your own objective. Does not create an alert or SLA."},
            {"name": "itl_objective_ms", "label": "ITL objective (ms)", "type": "textbox",
             "query": "100", "current": {"text": "100", "value": "100"}, "options": [],
             "description": "Illustrative reference line; enter your own objective. Does not create an alert or SLA."},
        ]},
        "panels": panels,
    })


if __name__ == "__main__":
    destination = Path(__file__).with_name("dynamo-efficiency-dashboard.json")
    destination.write_text(json.dumps(build_dashboard(), indent=2) + "\n")
    print(destination)
