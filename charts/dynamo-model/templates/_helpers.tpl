{{- define "dynamo-model.name" -}}
{{- required "model.name is required" .Values.model.name -}}
{{- end -}}

{{- define "dynamo-model.labels" -}}
app.kubernetes.io/name: {{ include "dynamo-model.name" . | quote }}
app.kubernetes.io/instance: {{ .Release.Name | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service | quote }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | quote }}
{{- if .Values.app.partOf }}
app.kubernetes.io/part-of: {{ .Values.app.partOf | quote }}
{{- end }}
{{- end -}}

{{- define "dynamo-model.redisSelectorLabels" -}}
app.kubernetes.io/name: {{ required "redis.name is required" .Values.redis.name | quote }}
{{- end -}}

{{- define "dynamo-model.plannerLegacyNamespacePatch" -}}
import runpy
from dynamo.planner.connectors.kubernetes import KubernetesConnector

def _base_worker_runtime_namespace(self, base_dynamo_namespace):
    return base_dynamo_namespace

KubernetesConnector.get_worker_runtime_namespace = _base_worker_runtime_namespace
runpy.run_module("dynamo.planner.__main__", run_name="__main__", alter_sys=True)
{{- end -}}

{{- define "dynamo-model.plannerConfigJson" -}}
{{- $p := .Values.planner -}}
{{- $cfg := dict
  "environment" $p.environment
  "backend" .Values.model.backendFramework
  "mode" $p.mode
  "optimization_target" $p.optimizationTarget
  "enable_throughput_scaling" $p.enableThroughputScaling
  "enable_load_scaling" $p.enableLoadScaling
  "pre_deployment_sweeping_mode" $p.preDeploymentSweepingMode
  "throughput_adjustment_interval_seconds" (int $p.throughputAdjustmentIntervalSeconds)
  "load_adjustment_interval_seconds" (int $p.loadAdjustmentIntervalSeconds)
  "throughput_metrics_source" $p.throughputMetricsSource
  "max_gpu_budget" (int $p.maxGpuBudget)
  "min_endpoint" (int $p.minEndpoint)
  "ttft_ms" (float64 $p.ttftMs)
  "itl_ms" (float64 $p.itlMs)
  "advisory" $p.advisory
}}
{{- if and (ne (toString $p.decodeScaleUpKvRate) "") (ne (toString $p.decodeScaleDownKvRate) "") -}}
{{- $_ := set $cfg "decode_scale_up_kv_rate" (float64 $p.decodeScaleUpKvRate) -}}
{{- $_ := set $cfg "decode_scale_down_kv_rate" (float64 $p.decodeScaleDownKvRate) -}}
{{- end -}}
{{- toJson $cfg -}}
{{- end -}}

{{- define "dynamo-model.storageClass" -}}
{{- if .storageClassName }}
storageClassName: {{ .storageClassName | quote }}
{{- end }}
{{- end -}}
