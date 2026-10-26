#!/usr/bin/env bash
set -euo pipefail

mode="${1:-all}"

APP_LABEL_VALUE="${APP_LABEL_VALUE:-daedalus-dynamo-demo}"
GATEWAY_NAME="${GATEWAY_NAME:-inference-gateway}"
GATEWAY_REQUEST_TIMEOUT="${GATEWAY_REQUEST_TIMEOUT:-300s}"
GATEWAY_PORT="${GATEWAY_PORT:-32359}"
GATEWAY_NODE_PORT="${GATEWAY_NODE_PORT:-32359}"
GATEWAY_LOAD_BALANCER_IP="${GATEWAY_LOAD_BALANCER_IP:-192.168.1.12}"
GATEWAY_IMAGE_REGISTRY="${GATEWAY_IMAGE_REGISTRY:-daedalus.ddns.me:5050}"
GATEWAY_IMAGE_REPOSITORY="${GATEWAY_IMAGE_REPOSITORY:-btuttle/daedalus-agentgateway}"
GATEWAY_IMAGE_TAG="${GATEWAY_IMAGE_TAG:-v1.4.1}"
GATEWAY_IMAGE_PULL_POLICY="${GATEWAY_IMAGE_PULL_POLICY:-IfNotPresent}"
GATEWAY_REPLICAS="${GATEWAY_REPLICAS:-2}"
GATEWAY_NOFILE_LIMIT="${GATEWAY_NOFILE_LIMIT:-131072}"
# With the 25m fd-metrics sidecar, retain the 510m total used by the CPU HPA.
GATEWAY_CPU_REQUEST="${GATEWAY_CPU_REQUEST:-485m}"
GATEWAY_CPU_LIMIT="${GATEWAY_CPU_LIMIT:-4}"
GATEWAY_MEMORY_REQUEST="${GATEWAY_MEMORY_REQUEST:-256Mi}"
GATEWAY_MEMORY_LIMIT="${GATEWAY_MEMORY_LIMIT:-2Gi}"
GATEWAY_HPA_MIN_REPLICAS="${GATEWAY_HPA_MIN_REPLICAS:-2}"
GATEWAY_HPA_MAX_REPLICAS="${GATEWAY_HPA_MAX_REPLICAS:-8}"
GATEWAY_HPA_CPU_UTILIZATION="${GATEWAY_HPA_CPU_UTILIZATION:-70}"
GATEWAY_POOL_IDLE_TIMEOUT="${GATEWAY_POOL_IDLE_TIMEOUT:-30s}"
GATEWAY_POOL_MAX_SIZE="${GATEWAY_POOL_MAX_SIZE:-64}"
GATEWAY_HTTP1_IDLE_TIMEOUT="${GATEWAY_HTTP1_IDLE_TIMEOUT:-60s}"
GATEWAY_MAX_CONNECTION_DURATION="${GATEWAY_MAX_CONNECTION_DURATION:-10m}"
GATEWAY_SHUTDOWN_MIN_SECONDS="${GATEWAY_SHUTDOWN_MIN_SECONDS:-15}"
GATEWAY_SHUTDOWN_MAX_SECONDS="${GATEWAY_SHUTDOWN_MAX_SECONDS:-330}"
GATEWAY_FD_EXPORTER_IMAGE="${GATEWAY_FD_EXPORTER_IMAGE:-python:3.12-alpine}"
GATEWAY_FD_METRICS_PORT="${GATEWAY_FD_METRICS_PORT:-9090}"
PROMETHEUS_RELEASE="${PROMETHEUS_RELEASE:-kube-prometheus-stack}"
MODEL_CATALOG_IMAGE="${MODEL_CATALOG_IMAGE:-python:3.12-alpine}"
MODELS_JSON="${MODELS_JSON:-}"
if [[ -z "${MODELS_JSON}" ]]; then
  MODELS_JSON='{"object":"list","data":[]}'
fi
MODEL_ROUTE_NAME="${MODEL_ROUTE_NAME:-}"
MODEL_ID="${MODEL_ID:-}"
MODEL_DGD="${MODEL_DGD:-}"

render_base() {
  local fd_metrics_hash
  fd_metrics_hash="$(sha256sum scripts/gateway-fd-metrics.py | awk '{print $1}')"
  cat <<EOF
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: ${GATEWAY_NAME}-fd-metrics
  labels:
    app.kubernetes.io/name: ${GATEWAY_NAME}-fd-metrics
    app.kubernetes.io/part-of: ${APP_LABEL_VALUE}
data:
  gateway-fd-metrics.py: |
$(sed 's/^/    /' scripts/gateway-fd-metrics.py)
---
apiVersion: agentgateway.dev/v1alpha1
kind: AgentgatewayParameters
metadata:
  name: ${GATEWAY_NAME}-params
  labels:
    app.kubernetes.io/name: ${GATEWAY_NAME}
    app.kubernetes.io/part-of: ${APP_LABEL_VALUE}
spec:
  image:
    registry: ${GATEWAY_IMAGE_REGISTRY}
    repository: ${GATEWAY_IMAGE_REPOSITORY}
    tag: ${GATEWAY_IMAGE_TAG}
    pullPolicy: ${GATEWAY_IMAGE_PULL_POLICY}
  env:
    - name: AGENTGATEWAY_NOFILE_LIMIT
      value: "${GATEWAY_NOFILE_LIMIT}"
  resources:
    requests:
      cpu: ${GATEWAY_CPU_REQUEST}
      memory: ${GATEWAY_MEMORY_REQUEST}
    limits:
      cpu: ${GATEWAY_CPU_LIMIT}
      memory: ${GATEWAY_MEMORY_LIMIT}
  shutdown:
    min: ${GATEWAY_SHUTDOWN_MIN_SECONDS}
    max: ${GATEWAY_SHUTDOWN_MAX_SECONDS}
  rawConfig:
    config:
      backend:
        poolIdleTimeout: ${GATEWAY_POOL_IDLE_TIMEOUT}
        poolMaxSize: ${GATEWAY_POOL_MAX_SIZE}
  deployment:
    spec:
      replicas: ${GATEWAY_REPLICAS}
      strategy:
        type: RollingUpdate
        rollingUpdate:
          maxUnavailable: 0
          maxSurge: 1
      template:
        metadata:
          annotations:
            daedalus-dynamo/fd-metrics-sha256: "${fd_metrics_hash}"
            sidecar.istio.io/inject: "false"
        spec:
          nodeSelector:
            kubernetes.io/arch: amd64
          shareProcessNamespace: true
          topologySpreadConstraints:
            - maxSkew: 1
              topologyKey: kubernetes.io/hostname
              whenUnsatisfiable: ScheduleAnyway
              labelSelector:
                matchLabels:
                  gateway.networking.k8s.io/gateway-name: ${GATEWAY_NAME}
          affinity:
            podAntiAffinity:
              preferredDuringSchedulingIgnoredDuringExecution:
                - weight: 100
                  podAffinityTerm:
                    topologyKey: kubernetes.io/hostname
                    labelSelector:
                      matchLabels:
                        gateway.networking.k8s.io/gateway-name: ${GATEWAY_NAME}
          containers:
            - name: fd-metrics
              image: ${GATEWAY_FD_EXPORTER_IMAGE}
              imagePullPolicy: Always
              command:
                - python3
                - /opt/daedalus/gateway-fd-metrics.py
              env:
                - name: PYTHONDONTWRITEBYTECODE
                  value: "1"
                - name: POD_NAME
                  valueFrom:
                    fieldRef:
                      fieldPath: metadata.name
                - name: POD_NAMESPACE
                  valueFrom:
                    fieldRef:
                      fieldPath: metadata.namespace
              ports:
                - name: fd-metrics
                  containerPort: ${GATEWAY_FD_METRICS_PORT}
                  protocol: TCP
              readinessProbe:
                httpGet:
                  path: /healthz
                  port: fd-metrics
                periodSeconds: 5
                timeoutSeconds: 2
                failureThreshold: 6
              livenessProbe:
                httpGet:
                  path: /metrics
                  port: fd-metrics
                periodSeconds: 30
                timeoutSeconds: 2
                failureThreshold: 3
              resources:
                requests:
                  cpu: 25m
                  memory: 32Mi
                limits:
                  cpu: 100m
                  memory: 96Mi
              securityContext:
                allowPrivilegeEscalation: false
                capabilities:
                  drop:
                    - ALL
                readOnlyRootFilesystem: true
                runAsNonRoot: true
                runAsUser: 10101
                runAsGroup: 10101
              volumeMounts:
                - name: fd-metrics-script
                  mountPath: /opt/daedalus
                  readOnly: true
          volumes:
            - name: fd-metrics-script
              configMap:
                name: ${GATEWAY_NAME}-fd-metrics
                defaultMode: 0444
  podDisruptionBudget:
    spec:
      minAvailable: 1
  horizontalPodAutoscaler:
    spec:
      minReplicas: ${GATEWAY_HPA_MIN_REPLICAS}
      maxReplicas: ${GATEWAY_HPA_MAX_REPLICAS}
      behavior:
        scaleUp:
          stabilizationWindowSeconds: 0
          selectPolicy: Max
          policies:
            - type: Percent
              value: 100
              periodSeconds: 30
            - type: Pods
              value: 2
              periodSeconds: 30
        scaleDown:
          stabilizationWindowSeconds: 300
          selectPolicy: Min
          policies:
            - type: Percent
              value: 25
              periodSeconds: 60
      metrics:
        - type: Resource
          resource:
            name: cpu
            target:
              type: Utilization
              averageUtilization: ${GATEWAY_HPA_CPU_UTILIZATION}
  service:
    metadata:
      annotations:
        metallb.io/loadBalancerIPs: "${GATEWAY_LOAD_BALANCER_IP}"
    spec:
      type: LoadBalancer
      allocateLoadBalancerNodePorts: true
      ports:
        - name: listener-${GATEWAY_PORT}
          port: ${GATEWAY_PORT}
          protocol: TCP
          targetPort: ${GATEWAY_PORT}
          nodePort: ${GATEWAY_NODE_PORT}
---
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: ${GATEWAY_NAME}
  labels:
    app.kubernetes.io/name: ${GATEWAY_NAME}
    app.kubernetes.io/part-of: ${APP_LABEL_VALUE}
spec:
  gatewayClassName: agentgateway
  infrastructure:
    parametersRef:
      group: agentgateway.dev
      kind: AgentgatewayParameters
      name: ${GATEWAY_NAME}-params
  listeners:
    - name: https
      port: ${GATEWAY_PORT}
      protocol: HTTPS
      tls:
        mode: Terminate
        certificateRefs:
          - group: ""
            kind: Secret
            name: daedalus-tls
            namespace: daedalus
---
apiVersion: agentgateway.dev/v1alpha1
kind: AgentgatewayPolicy
metadata:
  name: daedalus-dynamo-model-header
  labels:
    app.kubernetes.io/name: daedalus-dynamo-model-header
    app.kubernetes.io/part-of: ${APP_LABEL_VALUE}
spec:
  targetRefs:
    - group: gateway.networking.k8s.io
      kind: Gateway
      name: ${GATEWAY_NAME}
  frontend:
    http:
      http1IdleTimeout: ${GATEWAY_HTTP1_IDLE_TIMEOUT}
      maxConnectionDuration: ${GATEWAY_MAX_CONNECTION_DURATION}
  traffic:
    apiKeyAuthentication:
      mode: Strict
      secretRef:
        name: inference-gateway-api-keys
    phase: PreRouting
    transformation:
      conditional:
        - condition: request.method == "POST"
          policy:
            request:
              set:
                - name: X-Gateway-Base-Model-Name
                  value: 'json(request.body).model'
---
apiVersion: v1
kind: Service
metadata:
  name: ${GATEWAY_NAME}-fd-metrics
  labels:
    app.kubernetes.io/name: ${GATEWAY_NAME}-fd-metrics
    app.kubernetes.io/part-of: ${APP_LABEL_VALUE}
  annotations:
    prometheus.io/scrape: "true"
    prometheus.io/path: /metrics
    prometheus.io/port: "${GATEWAY_FD_METRICS_PORT}"
spec:
  type: ClusterIP
  selector:
    gateway.networking.k8s.io/gateway-name: ${GATEWAY_NAME}
  ports:
    - name: fd-metrics
      port: ${GATEWAY_FD_METRICS_PORT}
      targetPort: fd-metrics
      protocol: TCP
---
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: daedalus-dynamo-models
  labels:
    app.kubernetes.io/name: daedalus-dynamo-models
    app.kubernetes.io/part-of: ${APP_LABEL_VALUE}
spec:
  parentRefs:
    - group: gateway.networking.k8s.io
      kind: Gateway
      name: ${GATEWAY_NAME}
  rules:
    - matches:
        - path:
            type: Exact
            value: /v1/models
      backendRefs:
        - name: model-catalog
          port: 8000
      timeouts:
        request: ${GATEWAY_REQUEST_TIMEOUT}
EOF
}

render_monitoring() {
  cat <<EOF
---
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: ${GATEWAY_NAME}-fd-metrics
  labels:
    app.kubernetes.io/name: ${GATEWAY_NAME}-fd-metrics
    app.kubernetes.io/part-of: ${APP_LABEL_VALUE}
spec:
  namespaceSelector:
    matchNames:
      - ${NAMESPACE:-daedalus-dynamo}
  selector:
    matchLabels:
      app.kubernetes.io/name: ${GATEWAY_NAME}-fd-metrics
  endpoints:
    - port: fd-metrics
      path: /metrics
      interval: 10s
      scrapeTimeout: 5s
---
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: ${GATEWAY_NAME}-resilience
  labels:
    app.kubernetes.io/name: ${GATEWAY_NAME}
    app.kubernetes.io/part-of: ${APP_LABEL_VALUE}
    release: ${PROMETHEUS_RELEASE}
spec:
  groups:
    - name: daedalus-dynamo.gateway-resilience
      rules:
        - alert: DaedalusGatewayFileDescriptorHeadroomLow
          expr: daedalus_gateway_process_fd_utilization_ratio > 0.80
          for: 5m
          labels:
            severity: warning
          annotations:
            summary: Agentgateway file descriptor headroom is below 20 percent
            description: 'Pod {{ \$labels.namespace }}/{{ \$labels.pod }} is using {{ \$value | humanizePercentage }} of its RLIMIT_NOFILE soft limit.'
        - alert: DaedalusGatewayFileDescriptorsCritical
          expr: daedalus_gateway_process_fd_utilization_ratio > 0.90
          for: 2m
          labels:
            severity: critical
          annotations:
            summary: Agentgateway file descriptor headroom is below 10 percent
            description: 'Pod {{ \$labels.namespace }}/{{ \$labels.pod }} is close to EMFILE; stop increasing load and inspect socket growth.'
        - alert: DaedalusGatewayFDExporterDown
          expr: daedalus_gateway_fd_exporter_up == 0 or absent(daedalus_gateway_fd_exporter_up)
          for: 5m
          labels:
            severity: warning
          annotations:
            summary: Agentgateway FD telemetry is unavailable
            description: The gateway process is not visible to the FD metrics exporter or the exporter is not being scraped.
        - alert: DaedalusGatewayReplicasUnavailable
          expr: kube_deployment_status_replicas_available{namespace="${NAMESPACE:-daedalus-dynamo}",deployment="${GATEWAY_NAME}"} < ${GATEWAY_HPA_MIN_REPLICAS}
          for: 2m
          labels:
            severity: critical
          annotations:
            summary: Fewer than ${GATEWAY_HPA_MIN_REPLICAS} agentgateway replicas are available
            description: The inference endpoint has lost its configured high-availability replica floor.
EOF
}

render_route() {
  if [[ -z "${MODEL_ROUTE_NAME}" || -z "${MODEL_ID}" || -z "${MODEL_DGD}" ]]; then
    echo "MODEL_ROUTE_NAME, MODEL_ID, and MODEL_DGD are required for route mode." >&2
    exit 2
  fi

  cat <<EOF
---
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: ${MODEL_ROUTE_NAME}
  labels:
    app.kubernetes.io/name: ${MODEL_ROUTE_NAME}
    app.kubernetes.io/part-of: ${APP_LABEL_VALUE}
    daedalus-dynamo/route-kind: model
  annotations:
    daedalus-dynamo/model-id: "${MODEL_ID}"
    daedalus-dynamo/model-dgd: "${MODEL_DGD}"
spec:
  parentRefs:
    - group: gateway.networking.k8s.io
      kind: Gateway
      name: ${GATEWAY_NAME}
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /v1/
          headers:
            - type: Exact
              name: X-Gateway-Base-Model-Name
              value: "${MODEL_ID}"
      backendRefs:
        - name: ${MODEL_DGD}-frontend
          port: 8000
      timeouts:
        request: ${GATEWAY_REQUEST_TIMEOUT}
EOF
}

render_catalog() {
  local catalog_hash models_json_indented
  catalog_hash="$(printf '%s' "${MODELS_JSON}" | sha256sum | awk '{print $1}')"
  models_json_indented="$(printf '%s\n' "${MODELS_JSON}" | sed 's/^/    /')"

  cat <<EOF
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: model-catalog-config
  labels:
    app.kubernetes.io/name: model-catalog
    app.kubernetes.io/part-of: ${APP_LABEL_VALUE}
data:
  models.json: |
${models_json_indented}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: model-catalog
  labels:
    app.kubernetes.io/name: model-catalog
    app.kubernetes.io/part-of: ${APP_LABEL_VALUE}
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: model-catalog
  template:
    metadata:
      labels:
        app.kubernetes.io/name: model-catalog
        app.kubernetes.io/part-of: ${APP_LABEL_VALUE}
      annotations:
        daedalus-dynamo/models-json-sha256: "${catalog_hash}"
    spec:
      containers:
        - name: model-catalog
          image: ${MODEL_CATALOG_IMAGE}
          imagePullPolicy: Always
          resources:
            requests:
              cpu: 25m
              memory: 32Mi
          ports:
            - name: http
              containerPort: 8000
          env:
            - name: MODELS_JSON
              valueFrom:
                configMapKeyRef:
                  name: model-catalog-config
                  key: models.json
          command:
            - /bin/sh
            - -c
          args:
            - |
              cat > /tmp/model_catalog.py <<'PY'
              import json
              import os
              from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

              payload = os.environ.get("MODELS_JSON", '{"object":"list","data":[]}')
              json.loads(payload)

              class Handler(BaseHTTPRequestHandler):
                  def log_message(self, fmt, *args):
                      return

                  def do_GET(self):
                      if self.path.split("?", 1)[0] != "/v1/models":
                          self.send_response(404)
                          self.end_headers()
                          return
                      body = payload.encode("utf-8")
                      self.send_response(200)
                      self.send_header("content-type", "application/json")
                      self.send_header("content-length", str(len(body)))
                      self.end_headers()
                      self.wfile.write(body)

                  def do_OPTIONS(self):
                      self.send_response(204)
                      self.send_header("access-control-allow-origin", "*")
                      self.send_header("access-control-allow-methods", "GET,OPTIONS")
                      self.send_header("access-control-allow-headers", "content-type,authorization")
                      self.end_headers()

              ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
              PY
              exec python /tmp/model_catalog.py
          readinessProbe:
            httpGet:
              path: /v1/models
              port: http
            periodSeconds: 10
            timeoutSeconds: 5
          livenessProbe:
            httpGet:
              path: /v1/models
              port: http
            periodSeconds: 30
            timeoutSeconds: 5
---
apiVersion: v1
kind: Service
metadata:
  name: model-catalog
  labels:
    app.kubernetes.io/name: model-catalog
    app.kubernetes.io/part-of: ${APP_LABEL_VALUE}
spec:
  type: ClusterIP
  selector:
    app.kubernetes.io/name: model-catalog
  ports:
    - name: http
      port: 8000
      targetPort: 8000
EOF
}

case "${mode}" in
  base) render_base ;;
  route) render_route ;;
  catalog) render_catalog ;;
  monitoring) render_monitoring ;;
  all)
    render_base
    render_route
    render_catalog
    render_monitoring
    ;;
  *)
    echo "Usage: $0 [base|route|catalog|monitoring|all]" >&2
    exit 2
    ;;
esac
