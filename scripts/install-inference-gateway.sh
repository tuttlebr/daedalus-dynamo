#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${NAMESPACE:-daedalus-dynamo}"
AGENTGATEWAY_NAMESPACE="${AGENTGATEWAY_NAMESPACE:-agentgateway-system}"
GATEWAY_API_VERSION="${GATEWAY_API_VERSION:-v1.5.1}"
GAIE_VERSION="${GAIE_VERSION:-v1.5.0}"
AGENTGATEWAY_VERSION="${AGENTGATEWAY_VERSION:-v1.2.1}"
GATEWAY_NAME="${GATEWAY_NAME:-inference-gateway}"

echo "==> Installing Gateway API ${GATEWAY_API_VERSION}"
kubectl apply --server-side --force-conflicts \
  -f "https://github.com/kubernetes-sigs/gateway-api/releases/download/${GATEWAY_API_VERSION}/standard-install.yaml"

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
  --wait

kubectl create namespace "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -

echo "==> Creating ${GATEWAY_NAME} in namespace ${NAMESPACE}"
kubectl apply --server-side -n "${NAMESPACE}" -f - <<EOF
apiVersion: agentgateway.dev/v1alpha1
kind: AgentgatewayParameters
metadata:
  name: ${GATEWAY_NAME}-params
  labels:
    app.kubernetes.io/name: ${GATEWAY_NAME}
    app.kubernetes.io/part-of: daedalus-dynamo-demo
spec:
  deployment:
    spec:
      template:
        metadata:
          annotations:
            sidecar.istio.io/inject: "false"
  service:
    spec:
      type: LoadBalancer
      allocateLoadBalancerNodePorts: true
      ports:
        - name: listener-32359
          port: 32359
          protocol: TCP
          targetPort: 32359
          nodePort: 32359
---
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: ${GATEWAY_NAME}
  labels:
    app.kubernetes.io/name: ${GATEWAY_NAME}
    app.kubernetes.io/part-of: daedalus-dynamo-demo
spec:
  gatewayClassName: agentgateway
  infrastructure:
    parametersRef:
      group: agentgateway.dev
      kind: AgentgatewayParameters
      name: ${GATEWAY_NAME}-params
  listeners:
    - name: http
      port: 32359
      protocol: HTTP
EOF

kubectl wait "gateway/${GATEWAY_NAME}" \
  -n "${NAMESPACE}" \
  --for=condition=Programmed \
  --timeout=180s
