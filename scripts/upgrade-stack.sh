#!/usr/bin/env bash
# Replay the reviewed stack configuration without changing client endpoints.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a
source config/stack-versions.env
set +a
expected_context="${EXPECTED_CONTEXT:-kubernetes-admin@kubernetes}"
test "$(kubectl config current-context)" = "$expected_context"
umask 077
run_dir="${STACK_UPGRADE_DIR:-artifacts/stack-replay-$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$run_dir/before" "$run_dir/charts"
run_dir="$(realpath "$run_dir")"
helm list -A -o json > "$run_dir/before/helm.json"
kubectl get svc,gateway,httproute,ingress -A -o json > "$run_dir/before/endpoints.json"
kubectl get crd -o yaml > "$run_dir/before/crds.yaml"
kubectl get dgd -n daedalus-dynamo -o yaml > "$run_dir/before/models.yaml"
for entry in dynamo-system:dynamo-platform grove-system:grove kai-scheduler:kai-scheduler \
  agentgateway-system:agentgateway agentgateway-system:agentgateway-crds \
  gpu-operator:gpu-operator monitoring:kube-prometheus-stack \
  daedalus-dynamo:nemotron35bolt-gb10 daedalus-dynamo:qwen38-27b-l40s; do
  namespace="${entry%%:*}"
  release="${entry#*:}"
  helm get values "$release" -n "$namespace" -o yaml > "$run_dir/before/$release-values.yaml"
  helm get manifest "$release" -n "$namespace" > "$run_dir/before/$release-manifest.yaml"
done
kubectl get deploy lws-controller-manager -n lws-system -o json > "$run_dir/before/lws-deployment.json"
kubectl get all,configmap -n lws-system -o yaml > "$run_dir/before/lws.yaml"

# These custom images must already be built and pushed using the checked-in
# Dockerfiles. Validate them before changing the cluster.
for image in \
  daedalus.ddns.me:5050/btuttle/daedalus-nats:2.15.2 \
  daedalus.ddns.me:5050/btuttle/daedalus-agentgateway:v1.6.0 \
  daedalus.ddns.me:5050/btuttle/daedalus-dynamo-nemotron35bolt-gb10:1.6.0-vllm77154ca-mamba-prefill-20261027 \
  daedalus.ddns.me:5050/btuttle/daedalus-dynamo-nemotron35bolt-gb10:1.6.0-vllm77154ca-mamba-prefill-20261027-compat1; do
  docker buildx imagetools inspect "$image" > /dev/null
done
curl -fsSL "https://github.com/kai-scheduler/KAI-Scheduler/releases/download/$KAI_VERSION/kai-scheduler-$KAI_VERSION.tgz" \
  -o "$run_dir/charts/kai.tgz"
helm upgrade kai-scheduler "$run_dir/charts/kai.tgz" -n kai-scheduler \
  --reset-then-reuse-values -f config/kai-scheduler.yaml --atomic --wait --timeout 10m

# The existing scheduling workflow upgrades Grove before Dynamo, reconciles
# CRDs, restores controller requests, and checks the serving deployments.
bash scripts/install-scheduling-resilience.sh

curl -fsSL "https://github.com/kubernetes-sigs/lws/releases/download/$LWS_VERSION/manifests.yaml" \
  -o "$run_dir/charts/lws.yaml"
python3 - "$run_dir" <<'PY'
import json
import pathlib
import sys
import yaml

root = pathlib.Path(sys.argv[1])
old = json.loads((root / "before/lws-deployment.json").read_text())
objects = list(yaml.safe_load_all((root / "charts/lws.yaml").read_text()))
for obj in objects:
    if obj and obj.get("kind") == "Deployment":
        obj["spec"]["selector"] = old["spec"]["selector"]
        obj["spec"]["template"]["metadata"]["labels"].update(
            old["spec"]["selector"]["matchLabels"]
        )
        pod = obj["spec"]["template"]["spec"]
        pod["nodeSelector"] = {"kubernetes.io/arch": "amd64"}
        pod["containers"][0]["resources"] = {"requests": {"cpu": "100m", "memory": "128Mi"}}
(root / "lws.yaml").write_text(yaml.safe_dump_all(objects, sort_keys=False))
PY
kubectl apply --server-side --dry-run=server -f "$run_dir/lws.yaml" > /dev/null
kubectl apply --server-side -f "$run_dir/lws.yaml"
kubectl rollout status deployment/lws-controller-manager -n lws-system --timeout=5m

bash scripts/install-inference-gateway.sh
kubectl patch agentgatewayparameters inference-gateway-params -n daedalus-dynamo \
  --type=merge -p '{"spec":{"image":{"tag":"v1.6.0"}}}'
kubectl rollout status deployment/inference-gateway -n daedalus-dynamo --timeout=5m

curl -fsSL "https://helm.ngc.nvidia.com/nvidia/charts/gpu-operator-$GPU_OPERATOR_VERSION.tgz" \
  -o "$run_dir/charts/gpu.tgz"
helm upgrade gpu-operator "$run_dir/charts/gpu.tgz" -n gpu-operator \
  --reset-then-reuse-values -f config/gpu-operator.yaml --atomic --wait --timeout 15m
curl -fsSL "https://github.com/prometheus-community/helm-charts/releases/download/kube-prometheus-stack-$MONITORING_CHART_VERSION/kube-prometheus-stack-$MONITORING_CHART_VERSION.tgz" \
  -o "$run_dir/charts/monitoring.tgz"
# The monitoring Services intentionally retain their existing LoadBalancer
# type, fixed NodePorts and source ranges, although no external IP is assigned.
# Helm --wait would wait forever on those Services. Check workloads explicitly.
helm upgrade kube-prometheus-stack "$run_dir/charts/monitoring.tgz" -n monitoring \
  --reset-then-reuse-values -f config/kube-prometheus-stack.yaml --timeout 10m
kubectl rollout status deployment -n monitoring --timeout=10m
kubectl wait prometheus/kube-prometheus-maintenance -n monitoring --for=condition=Available --timeout=10m

helm upgrade nemotron35bolt-gb10 charts/dynamo-model -n daedalus-dynamo \
  -f values/nemotron35bolt-gb10.yaml --wait --timeout 10m
helm upgrade qwen38-27b-l40s charts/dynamo-model -n daedalus-dynamo \
  -f values/staging/qwen36-35b-fp8-l40s.yaml \
  --post-renderer scripts/qwen36_fp8_postrender.py --wait --timeout 10m
kubectl wait dgd -n daedalus-dynamo --all --for=condition=Ready --timeout=40m
helm list -A -o json > "$run_dir/helm-after.json"
kubectl get svc,gateway,httproute,ingress -A -o json > "$run_dir/endpoints-after.json"
printf 'Stack rollout complete. Run LAN and application API checks before acceptance. Evidence: %s\n' "$run_dir"
