#!/usr/bin/env bash
set -euo pipefail

AGENTGATEWAY_NAMESPACE="${AGENTGATEWAY_NAMESPACE:-agentgateway-system}"
GATEWAY_API_VERSION="${GATEWAY_API_VERSION:-v1.5.0}"
GATEWAY_API_CHANNEL="${GATEWAY_API_CHANNEL:-experimental}"
GAIE_VERSION="${GAIE_VERSION:-v1.5.0}"
AGENTGATEWAY_VERSION="${AGENTGATEWAY_VERSION:-v1.3.1}"
AGENTGATEWAY_CONTROLLER_REPLICAS="${AGENTGATEWAY_CONTROLLER_REPLICAS:-2}"
AGENTGATEWAY_CONTROLLER_MAX_REPLICAS="${AGENTGATEWAY_CONTROLLER_MAX_REPLICAS:-4}"
AGENTGATEWAY_CONTROLLER_CPU_REQUEST="${AGENTGATEWAY_CONTROLLER_CPU_REQUEST:-250m}"
AGENTGATEWAY_CONTROLLER_CPU_LIMIT="${AGENTGATEWAY_CONTROLLER_CPU_LIMIT:-2}"
AGENTGATEWAY_CONTROLLER_MEMORY_REQUEST="${AGENTGATEWAY_CONTROLLER_MEMORY_REQUEST:-256Mi}"
AGENTGATEWAY_CONTROLLER_MEMORY_LIMIT="${AGENTGATEWAY_CONTROLLER_MEMORY_LIMIT:-1Gi}"

ensure_crd_version_served() {
  local crd="$1" version="$2" index served
  if ! kubectl get crd "${crd}" >/dev/null 2>&1; then
    echo "ERROR: required CRD ${crd} is not installed." >&2
    return 1
  fi
  index="$(
    kubectl get crd "${crd}" -o json |
      python3 -c 'import json,sys; version=sys.argv[1]; data=json.load(sys.stdin); print(next((i for i,v in enumerate(data["spec"]["versions"]) if v["name"] == version), ""))' "${version}"
  )"
  if [[ -z "${index}" ]]; then
    echo "ERROR: ${crd} does not contain ${version}; agentgateway ${AGENTGATEWAY_VERSION} cannot start with this Gateway API bundle." >&2
    return 1
  fi
  served="$(kubectl get crd "${crd}" -o jsonpath="{.spec.versions[${index}].served}")"
  if [[ "${served}" != "true" ]]; then
    echo "==> Enabling retained ${version} compatibility endpoint on ${crd}"
    kubectl patch crd "${crd}" --type=json \
      -p="[{\"op\":\"replace\",\"path\":\"/spec/versions/${index}/served\",\"value\":true}]" >/dev/null
  fi
}

current_gateway_api_version="$(
  kubectl get crd gateways.gateway.networking.k8s.io \
    -o jsonpath='{.metadata.annotations.gateway\.networking\.k8s\.io/bundle-version}' \
    2>/dev/null || true
)"
if [[ -z "${current_gateway_api_version}" || "${current_gateway_api_version}" == "${GATEWAY_API_VERSION}" ]] ||
  [[ "$(printf '%s\n%s\n' "${current_gateway_api_version}" "${GATEWAY_API_VERSION}" | sort -V | head -1)" == "${current_gateway_api_version}" ]]; then
  echo "==> Installing Gateway API ${GATEWAY_API_VERSION} (${GATEWAY_API_CHANNEL})"
  kubectl apply --server-side --force-conflicts \
    -f "https://github.com/kubernetes-sigs/gateway-api/releases/download/${GATEWAY_API_VERSION}/${GATEWAY_API_CHANNEL}-install.yaml"
else
  echo "==> Preserving newer Gateway API ${current_gateway_api_version}; validating agentgateway compatibility endpoints"
fi

# Agentgateway v1.3.1 watches TCPRoute through the v1alpha2 client. Gateway API
# v1.6 retains the schema but disables that served version, so preserve newer
# CRDs and re-enable only the compatibility endpoint instead of downgrading.
ensure_crd_version_served tcproutes.gateway.networking.k8s.io v1alpha2

echo "==> Installing Gateway API Inference Extension ${GAIE_VERSION}"
kubectl apply \
  -f "https://github.com/kubernetes-sigs/gateway-api-inference-extension/releases/download/${GAIE_VERSION}/manifests.yaml"

echo "==> Installing agentgateway ${AGENTGATEWAY_VERSION}"
helm upgrade -i \
  --create-namespace \
  --namespace "${AGENTGATEWAY_NAMESPACE}" \
  --version "${AGENTGATEWAY_VERSION}" \
  agentgateway-crds \
  oci://cr.agentgateway.dev/charts/agentgateway-crds

helm upgrade -i \
  --namespace "${AGENTGATEWAY_NAMESPACE}" \
  --version "${AGENTGATEWAY_VERSION}" \
  agentgateway \
  oci://cr.agentgateway.dev/charts/agentgateway \
  --set inferenceExtension.enabled=true \
  --set-string proxy.image.tag="${AGENTGATEWAY_VERSION}" \
  --set controller.replicaCount="${AGENTGATEWAY_CONTROLLER_REPLICAS}" \
  --set controller.podDisruptionBudget.minAvailable=1 \
  --set controller.horizontalPodAutoscaler.minReplicas="${AGENTGATEWAY_CONTROLLER_REPLICAS}" \
  --set controller.horizontalPodAutoscaler.maxReplicas="${AGENTGATEWAY_CONTROLLER_MAX_REPLICAS}" \
  --set controller.horizontalPodAutoscaler.metrics[0].type=Resource \
  --set controller.horizontalPodAutoscaler.metrics[0].resource.name=cpu \
  --set controller.horizontalPodAutoscaler.metrics[0].resource.target.type=Utilization \
  --set controller.horizontalPodAutoscaler.metrics[0].resource.target.averageUtilization=80 \
  --set resources.requests.cpu="${AGENTGATEWAY_CONTROLLER_CPU_REQUEST}" \
  --set resources.requests.memory="${AGENTGATEWAY_CONTROLLER_MEMORY_REQUEST}" \
  --set resources.limits.cpu="${AGENTGATEWAY_CONTROLLER_CPU_LIMIT}" \
  --set resources.limits.memory="${AGENTGATEWAY_CONTROLLER_MEMORY_LIMIT}" \
  --atomic \
  --wait \
  --timeout 10m

kubectl rollout status deployment/agentgateway \
  --namespace "${AGENTGATEWAY_NAMESPACE}" \
  --timeout=5m

echo "==> Agentgateway control plane ${AGENTGATEWAY_VERSION} is ready; gateway resources are applied by make gateway-base"
