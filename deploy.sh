#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
REPO_ROOT="$(pwd)"

ACTION="${ACTION:-deploy}"
declare -a REQUESTED_BACKENDS=()

if [[ "$#" -gt 0 ]]; then
  case "$1" in
    deploy|delete|render|install-gateway|doctor|-h|--help|help)
      ACTION="$1"
      shift
      ;;
  esac
fi

if [[ "$#" -gt 0 ]]; then
  REQUESTED_BACKENDS=("$@")
elif [[ -n "${BACKENDS:-}" ]]; then
  IFS=',' read -r -a REQUESTED_BACKENDS <<< "${BACKENDS}"
elif [[ "${ACTION}" == "delete" ]]; then
  REQUESTED_BACKENDS=("all")
else
  REQUESTED_BACKENDS=("qwen35b")
fi

REGISTRY="${REGISTRY:-daedalus.ddns.me:5050/btuttle}"
if [[ -z "${IMAGE_TAG:-}" ]]; then
  IMAGE_TAG="latest"
fi

NAMESPACE="${NAMESPACE:-daedalus-dynamo}"
GATEWAY_NAME="${GATEWAY_NAME:-inference-gateway}"
HF_TOKEN_SECRET="${HF_TOKEN_SECRET:-hf-token-secret}"
WORKBENCH_AUTH_SECRET="${WORKBENCH_AUTH_SECRET:-routing-workbench-auth}"
APP_LABEL_VALUE="daedalus-dynamo-demo"
APP_LABEL="app.kubernetes.io/part-of=${APP_LABEL_VALUE}"
PREFLIGHT_TIMEOUT_SECONDS="${PREFLIGHT_TIMEOUT_SECONDS:-20}"
WORKBENCH_NODEPORT="${WORKBENCH_NODEPORT:-30153}"
GATEWAY_REQUEST_TIMEOUT="${GATEWAY_REQUEST_TIMEOUT:-300s}"
MODEL_CATALOG_IMAGE="${MODEL_CATALOG_IMAGE:-python:3.12-alpine}"
RUN_DOCTOR="${RUN_DOCTOR:-0}"
DOCTOR_STRICT="${DOCTOR_STRICT:-0}"
DELETE_WAIT_SECONDS="${DELETE_WAIT_SECONDS:-120}"

NEMOTRON_IMAGE_REPOSITORY="${NEMOTRON_IMAGE_REPOSITORY:-${REGISTRY}/nemotron-3-nano-omni}"
QWEN35B_IMAGE_REPOSITORY="${QWEN35B_IMAGE_REPOSITORY:-${REGISTRY}/qwen36-35b-dynamo}"
QWEN27B_IMAGE_REPOSITORY="${QWEN27B_IMAGE_REPOSITORY:-${REGISTRY}/qwen36-27b-dynamo}"
GEMMA431B_IMAGE_REPOSITORY="${GEMMA431B_IMAGE_REPOSITORY:-${REGISTRY}/gemma-4-31b-it-dynamo}"
NEMOTRONNANO_IMAGE_REPOSITORY="${NEMOTRONNANO_IMAGE_REPOSITORY:-${REGISTRY}/nemotron-3-nano-30b-a3b-nvfp4-dynamo}"
WORKBENCH_IMAGE_REPOSITORY="${WORKBENCH_IMAGE_REPOSITORY:-${REGISTRY}/routing-workbench}"

NEMOTRON_IMAGE="${NEMOTRON_IMAGE_REPOSITORY}:${IMAGE_TAG}"
QWEN35B_IMAGE="${QWEN35B_IMAGE_REPOSITORY}:${IMAGE_TAG}"
QWEN27B_IMAGE="${QWEN27B_IMAGE_REPOSITORY}:${IMAGE_TAG}"
GEMMA431B_IMAGE="${GEMMA431B_IMAGE_REPOSITORY}:${IMAGE_TAG}"
NEMOTRONNANO_IMAGE="${NEMOTRONNANO_IMAGE_REPOSITORY}:${IMAGE_TAG}"
WORKBENCH_IMAGE="${WORKBENCH_IMAGE_REPOSITORY}:${IMAGE_TAG}"

usage() {
  cat <<EOF
Usage: $0 [deploy|delete|render|install-gateway|doctor] [backend...]

Subcommands:
  deploy           build/push images and apply the GAIE deployment (default)
  render           print the rendered manifests without applying
  delete           delete rendered resources (keeps data unless DELETE_DATA=1)
  install-gateway  install/upgrade Gateway API, GAIE, and agentgateway
  doctor           run read-only cluster-readiness checks (incl. RDMA/InfiniBand)

Backends:
  nemotron     nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-NVFP4 on vLLM
  qwen35b      Qwen/Qwen3.6-35B-A3B-FP8 on vLLM
  qwen27b      Qwen/Qwen3.6-27B-FP8 on SGLang
  gemma431b    google/gemma-4-31B-it on vLLM
  nemotronnano nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4 on vLLM
  all          Deploy or delete all backends

Examples:
  ./deploy.sh doctor
  HF_TOKEN=... ./deploy.sh deploy
  HF_TOKEN=... ./deploy.sh deploy qwen35b qwen27b
  HF_TOKEN=... ./deploy.sh deploy gemma431b
  HF_TOKEN=... ./deploy.sh deploy nemotronnano
  BACKENDS=nemotron,gemma431b,nemotronnano ./deploy.sh render
  ./deploy.sh delete
  ./deploy.sh delete nemotron
  INSTALL_GATEWAY=1 HF_TOKEN=... ./deploy.sh deploy all

Common environment:
  NAMESPACE                    default: daedalus-dynamo
  REGISTRY                     default: daedalus.ddns.me:5050/btuttle
  IMAGE_TAG                    default: UTC timestamp
  SKIP_BUILD=1                 skip docker builds
  SKIP_PUSH=1                  skip docker pushes
  SKIP_DEPLOY=1                build/push only
  INSTALL_GATEWAY=1            install/upgrade Gateway API, GAIE, and agentgateway first
  DEPLOY_WORKBENCH=0           gateway/backends only by default; set 1 to deploy the shared Dynamo Routing Workbench
  DEPLOY_PHOTO_TAGGER=<0|1>    compatibility alias for DEPLOY_WORKBENCH
  DEFAULT_BACKEND=<backend>    Workbench model backend; defaults to first selected backend
  WORKBENCH_DEFAULT_MODEL=<id> Workbench model override; defaults from DEFAULT_BACKEND
  WORKBENCH_NODEPORT=30153     shared Workbench UI NodePort
  GEMMA431B_IMAGE_REPOSITORY=<image-repo> Gemma 4 31B backend image repository
  NEMOTRONNANO_IMAGE_REPOSITORY=<image-repo> Nemotron Nano text backend image repository
  RUN_DOCTOR=1                 run the cluster doctor before deploy (advisory)
  DOCTOR_STRICT=1              abort deploy if the doctor reports failures
  DELETE_WAIT_SECONDS=120      wait for operator-owned backend resources during delete; set 0 to skip

Backend worker replicas, GPU counts, runtime args, direct services, PVCs, and
transport settings come from each selected folder's k8s/base YAML.
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

validate_positive_int() {
  local name="$1"
  local value="$2"
  [[ "${value}" =~ ^[1-9][0-9]*$ ]] || die "${name} must be a positive integer (got '${value}')."
}

validate_nodeport() {
  local name="$1"
  local value="$2"
  [[ "${value}" =~ ^[0-9]+$ ]] || die "${name} must be a numeric NodePort (got '${value}')."
  ((value >= 30000 && value <= 32767)) ||
    die "${name} must be in the default Kubernetes NodePort range 30000-32767 (got '${value}')."
}

validate_serving_config() {
  if workbench_enabled; then
    validate_nodeport WORKBENCH_NODEPORT "${WORKBENCH_NODEPORT}"
  fi
}

normalize_backend() {
  case "${1,,}" in
    all) printf 'all\n' ;;
    nemotron|nemotron-omni|omni) printf 'nemotron\n' ;;
    qwen35b|qwen-35b|qwen36-35b|35b) printf 'qwen35b\n' ;;
    qwen27b|qwen-27b|qwen36-27b|27b|sglang) printf 'qwen27b\n' ;;
    gemma431b|gemma4-31b|gemma-4-31b-it|google-gemma-4-31b-it) printf 'gemma431b\n' ;;
    nemotronnano|nemotron-nano|nemotron3nano|nvidia-nemotron-3-nano-30b-a3b-nvfp4) printf 'nemotronnano\n' ;;
    *) die "Unknown backend: $1" ;;
  esac
}

select_backends() {
  local requested normalized backend
  local -A seen=()
  SELECTED_BACKENDS=()

  for requested in "${REQUESTED_BACKENDS[@]}"; do
    [[ -n "${requested}" ]] || continue
    normalized="$(normalize_backend "${requested}")"
    if [[ "${normalized}" == "all" ]]; then
      SELECTED_BACKENDS=(nemotron qwen35b qwen27b gemma431b nemotronnano)
      return
    fi
    if [[ -z "${seen[${normalized}]:-}" ]]; then
      SELECTED_BACKENDS+=("${normalized}")
      seen["${normalized}"]=1
    fi
  done

  [[ "${#SELECTED_BACKENDS[@]}" -gt 0 ]] || die "No backends selected."

  for backend in "${SELECTED_BACKENDS[@]}"; do
    case "${backend}" in
      nemotron|qwen35b|qwen27b|gemma431b|nemotronnano) ;;
      *) die "Internal backend normalization error: ${backend}" ;;
    esac
  done
}

backend_model() {
  backend_manifest_value "$1" model
}

backend_dgd() {
  backend_manifest_value "$1" dgd
}

backend_image() {
  case "$1" in
    nemotron) printf '%s\n' "${NEMOTRON_IMAGE}" ;;
    qwen35b) printf '%s\n' "${QWEN35B_IMAGE}" ;;
    qwen27b) printf '%s\n' "${QWEN27B_IMAGE}" ;;
    gemma431b) printf '%s\n' "${GEMMA431B_IMAGE}" ;;
    nemotronnano) printf '%s\n' "${NEMOTRONNANO_IMAGE}" ;;
  esac
}

backend_build_dir() {
  case "$1" in
    nemotron) printf '%s\n' "nemotron-3-nano-omni-dynamo" ;;
    qwen35b) printf '%s\n' "qwen-36-35b-dynamo" ;;
    qwen27b) printf '%s\n' "qwen-36-27b-dynamo" ;;
    gemma431b) printf '%s\n' "gemma-4-31b-it-dynamo" ;;
    nemotronnano) printf '%s\n' "nemotron-3-nano-30b-a3b-nvfp4-dynamo" ;;
  esac
}

backend_build_arg() {
  case "$1" in
    nemotron) printf '%s\n' "${NEMOTRON_BASE_IMAGE:-${BASE_IMAGE:-}}" ;;
    qwen35b) printf '%s\n' "${QWEN35B_BASE_IMAGE:-${BASE_IMAGE:-}}" ;;
    qwen27b) printf '%s\n' "${QWEN27B_BASE_IMAGE:-${BASE_IMAGE:-}}" ;;
    gemma431b) printf '%s\n' "${GEMMA431B_BASE_IMAGE:-${BASE_IMAGE:-}}" ;;
    nemotronnano) printf '%s\n' "${NEMOTRONNANO_BASE_IMAGE:-${BASE_IMAGE:-}}" ;;
  esac
}

backend_kustomize_dir() {
  printf '%s/k8s/base\n' "$(backend_build_dir "$1")"
}

kustomize_build() {
  local dir="$1"
  if command -v kubectl > /dev/null 2>&1; then
    kubectl kustomize "${dir}"
    return
  fi
  if command -v kustomize > /dev/null 2>&1; then
    kustomize build "${dir}"
    return
  fi
  die "kubectl or kustomize is required to render backend YAML from ${dir}."
}

render_backend() {
  printf '%s\n' '---'
  kustomize_build "$(backend_kustomize_dir "$1")"
}

backend_manifest_value() {
  local backend="$1"
  local field="$2"
  local value

  case "${field}" in
    dgd)
      value="$(
        render_backend "${backend}" | awk '
          /^---/ { in_dgd = 0; in_meta = 0 }
          $1 == "kind:" && $2 == "DynamoGraphDeployment" { in_dgd = 1; next }
          in_dgd && $1 == "metadata:" { in_meta = 1; next }
          in_dgd && in_meta && $1 == "name:" { print $2; exit }
        '
      )"
      ;;
    model)
      value="$(
        render_backend "${backend}" | awk '
          $1 == "-" && $2 == "name:" && $3 == "MODEL_NAME" { want = 1; next }
          want && $1 == "value:" {
            $1 = ""
            sub(/^[[:space:]]*/, "")
            gsub(/^"|"$/, "")
            print
            exit
          }
        '
      )"
      ;;
    *) die "Internal manifest lookup error: ${field}" ;;
  esac

  [[ -n "${value}" ]] || die "Could not read ${field} for backend ${backend} from $(backend_kustomize_dir "${backend}")."
  printf '%s\n' "${value}"
}

default_backend() {
  local backend="${DEFAULT_BACKEND:-${SELECTED_BACKENDS[0]}}"
  normalize_backend "${backend}"
}

default_model() {
  if [[ -n "${WORKBENCH_DEFAULT_MODEL:-}" ]]; then
    printf '%s\n' "${WORKBENCH_DEFAULT_MODEL}"
    return
  fi
  if [[ -n "${DEFAULT_MODEL:-}" ]]; then
    printf '%s\n' "${DEFAULT_MODEL}"
    return
  fi
  backend_model "$(default_backend)"
}

workbench_enabled() {
  local value="${DEPLOY_WORKBENCH:-}"
  if [[ -z "${value}" && -n "${DEPLOY_PHOTO_TAGGER:-}" ]]; then
    value="${DEPLOY_PHOTO_TAGGER}"
  fi
  value="${value:-0}"
  [[ "${value,,}" != "0" && "${value,,}" != "false" && "${value,,}" != "no" ]]
}

json_escape() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  printf '%s' "${value}"
}

models_json() {
  local backend model first=1
  printf '{"object":"list","data":['
  for backend in "${SELECTED_BACKENDS[@]}"; do
    model="$(backend_model "${backend}")"
    if [[ "${first}" -eq 0 ]]; then
      printf ','
    fi
    first=0
    printf '{"id":"%s","object":"model","created":0,"owned_by":"daedalus"}' "$(json_escape "${model}")"
  done
  printf ']}'
}

api_resource_exists() {
  # Every prerequisite we probe is a CRD; query it by name. This avoids the
  # flaky `kubectl api-resources` scan, which can return a partial list when an
  # aggregated API on the cluster is briefly unavailable and cause false misses.
  kubectl get crd "$1" > /dev/null 2>&1
}

operator_rollout_available() {
  local namespace="$1"
  local deployment="$2"

  kubectl get deployment "${deployment}" -n "${namespace}" > /dev/null 2>&1 &&
    kubectl rollout status "deployment/${deployment}" \
      -n "${namespace}" \
      --timeout="${PREFLIGHT_TIMEOUT_SECONDS}s" \
      > /dev/null
}

preflight_cluster() {
  echo "==> Checking Kubernetes and Dynamo prerequisites"
  kubectl version --request-timeout="${PREFLIGHT_TIMEOUT_SECONDS}s" > /dev/null

  api_resource_exists "dynamographdeployments.nvidia.com" ||
    die "DynamoGraphDeployment CRD is not installed."
  api_resource_exists "podcliquesets.grove.io" ||
    die "Grove PodCliqueSet CRD is not installed."
  api_resource_exists "gateways.gateway.networking.k8s.io" ||
    die "Gateway API CRDs are not installed. Run INSTALL_GATEWAY=1 $0 deploy ${SELECTED_BACKENDS[*]} or $0 install-gateway."
  api_resource_exists "httproutes.gateway.networking.k8s.io" ||
    die "HTTPRoute CRD is not installed."
  api_resource_exists "inferencepools.inference.networking.k8s.io" ||
    die "Gateway API Inference Extension CRDs are not installed."
  api_resource_exists "agentgatewaypolicies.agentgateway.dev" ||
    die "agentgateway CRDs are not installed."

  operator_rollout_available "dynamo-system" "dynamo-platform-dynamo-operator-controller-manager" ||
    die "Dynamo operator deployment is not available in namespace dynamo-system."
  operator_rollout_available "grove-system" "grove-operator" ||
    die "Grove operator deployment is not available in namespace grove-system."
}

install_gateway() {
  NAMESPACE="${NAMESPACE}" \
  GATEWAY_NAME="${GATEWAY_NAME}" \
  "${REPO_ROOT}/scripts/install-inference-gateway.sh"
}

ensure_namespace() {
  kubectl create namespace "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -
}

ensure_hf_token_secret() {
  ensure_namespace

  if [[ -n "${HF_TOKEN:-}" ]]; then
    echo "==> Creating/updating ${HF_TOKEN_SECRET} in namespace ${NAMESPACE}"
    kubectl create secret generic "${HF_TOKEN_SECRET}" \
      --from-literal=HF_TOKEN="${HF_TOKEN}" \
      -n "${NAMESPACE}" \
      --dry-run=client -o yaml | kubectl apply -f -
    kubectl label secret "${HF_TOKEN_SECRET}" -n "${NAMESPACE}" "${APP_LABEL}" --overwrite > /dev/null
    return
  fi

  if kubectl get secret "${HF_TOKEN_SECRET}" -n "${NAMESPACE}" > /dev/null 2>&1; then
    echo "==> Reusing existing ${HF_TOKEN_SECRET} in namespace ${NAMESPACE}"
    kubectl label secret "${HF_TOKEN_SECRET}" -n "${NAMESPACE}" "${APP_LABEL}" --overwrite > /dev/null
    return
  fi

  die "${HF_TOKEN_SECRET} does not exist in namespace ${NAMESPACE}. Set HF_TOKEN or create the secret first."
}

generate_secret_value() {
  if command -v openssl > /dev/null 2>&1; then
    openssl rand -base64 32
    return
  fi
  od -An -N24 -tx1 /dev/urandom | tr -d ' \n'
  printf '\n'
}

ensure_workbench_auth_secret() {
  ensure_namespace

  if kubectl get secret "${WORKBENCH_AUTH_SECRET}" -n "${NAMESPACE}" > /dev/null 2>&1; then
    echo "==> Reusing existing ${WORKBENCH_AUTH_SECRET} in namespace ${NAMESPACE}"
    kubectl label secret "${WORKBENCH_AUTH_SECRET}" -n "${NAMESPACE}" "${APP_LABEL}" --overwrite > /dev/null
    return
  fi

  echo "==> Creating ${WORKBENCH_AUTH_SECRET} in namespace ${NAMESPACE}"
  kubectl create secret generic "${WORKBENCH_AUTH_SECRET}" \
    --from-literal=token="${WORKBENCH_AUTH_TOKEN:-$(generate_secret_value)}" \
    -n "${NAMESPACE}" \
    --dry-run=client -o yaml | kubectl apply -f -
  kubectl label secret "${WORKBENCH_AUTH_SECRET}" -n "${NAMESPACE}" "${APP_LABEL}" --overwrite > /dev/null
}

build_images() {
  local backend dir image base_image

  if [[ "${SKIP_BUILD:-0}" == "1" ]]; then
    return
  fi

  for backend in "${SELECTED_BACKENDS[@]}"; do
    dir="$(backend_build_dir "${backend}")"
    image="$(backend_image "${backend}")"
    base_image="$(backend_build_arg "${backend}")"

    build_args=()
    if [[ -n "${base_image}" ]]; then
      build_args+=(--build-arg "BASE_IMAGE=${base_image}")
    fi

    echo "==> Building ${image}"
    docker build "${build_args[@]}" -t "${image}" -f "${dir}/Dockerfile" "${dir}"
  done

  if workbench_enabled; then
    echo "==> Building ${WORKBENCH_IMAGE}"
    docker build -t "${WORKBENCH_IMAGE}" \
      -f routing-workbench/Dockerfile \
      routing-workbench
  fi
}

push_images() {
  local backend image

  if [[ "${SKIP_PUSH:-0}" == "1" ]]; then
    return
  fi

  for backend in "${SELECTED_BACKENDS[@]}"; do
    image="$(backend_image "${backend}")"
    echo "==> Pushing ${image}"
    docker push "${image}"
  done

  if workbench_enabled; then
    echo "==> Pushing ${WORKBENCH_IMAGE}"
    docker push "${WORKBENCH_IMAGE}"
  fi
}

render_model_catalog() {
  local catalog_json catalog_hash
  catalog_json="$(models_json)"
  catalog_hash="$(printf '%s' "${catalog_json}" | sha256sum | awk '{print $1}')"

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
    ${catalog_json}
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
          imagePullPolicy: IfNotPresent
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

render_gateway_resources() {
  cat <<EOF
---
apiVersion: agentgateway.dev/v1alpha1
kind: AgentgatewayParameters
metadata:
  name: ${GATEWAY_NAME}-params
  labels:
    app.kubernetes.io/name: ${GATEWAY_NAME}
    app.kubernetes.io/part-of: ${APP_LABEL_VALUE}
spec:
  deployment:
    spec:
      template:
        metadata:
          annotations:
            sidecar.istio.io/inject: "false"
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
    - name: http
      port: 80
      protocol: HTTP
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
  traffic:
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
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: daedalus-dynamo
  labels:
    app.kubernetes.io/name: daedalus-dynamo
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

  local backend model dgd
  for backend in "${SELECTED_BACKENDS[@]}"; do
    model="$(backend_model "${backend}")"
    dgd="$(backend_dgd "${backend}")"
    cat <<EOF
    - matches:
        - path:
            type: PathPrefix
            value: /v1/
          headers:
            - type: Exact
              name: X-Gateway-Base-Model-Name
              value: ${model}
      backendRefs:
        - name: ${dgd}-frontend
          port: 8000
      timeouts:
        request: ${GATEWAY_REQUEST_TIMEOUT}
EOF
  done
}

render_workbench() {
  local selected_model openai_base_url
  selected_model="$(default_model)"
  openai_base_url="${OPENAI_BASE_URL:-http://${GATEWAY_NAME}:80/v1}"

  cat <<EOF
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: routing-workbench-config
  labels:
    app.kubernetes.io/name: routing-workbench
    app.kubernetes.io/part-of: ${APP_LABEL_VALUE}
data:
  LISTEN_ADDR: 0.0.0.0:3000
  OPENAI_BASE_URL: ${openai_base_url}
  WORKBENCH_DEFAULT_MODEL: ${selected_model}
  WORKBENCH_REQUIRE_AUTH: "${WORKBENCH_REQUIRE_AUTH:-true}"
  WORKBENCH_AUTH_USER: ${WORKBENCH_AUTH_USER:-routing-workbench}
  WORKBENCH_REQUEST_TIMEOUT_SECS: "${WORKBENCH_REQUEST_TIMEOUT_SECS:-300}"
  WORKBENCH_MAX_RUN_REQUESTS: "${WORKBENCH_MAX_RUN_REQUESTS:-128}"
  WORKBENCH_MAX_CONCURRENCY: "${WORKBENCH_MAX_CONCURRENCY:-16}"
  RUST_LOG: ${RUST_LOG:-info,routing_workbench=info}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: routing-workbench
  labels:
    app.kubernetes.io/name: routing-workbench
    app.kubernetes.io/part-of: ${APP_LABEL_VALUE}
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: routing-workbench
  template:
    metadata:
      labels:
        app.kubernetes.io/name: routing-workbench
        app.kubernetes.io/part-of: ${APP_LABEL_VALUE}
    spec:
      securityContext:
        runAsUser: 977
        runAsGroup: 988
        runAsNonRoot: true
        fsGroup: 988
        fsGroupChangePolicy: OnRootMismatch
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: routing-workbench
          image: ${WORKBENCH_IMAGE}
          imagePullPolicy: Always
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop:
                - ALL
          ports:
            - name: http
              containerPort: 3000
          env:
            - name: LISTEN_ADDR
              valueFrom:
                configMapKeyRef:
                  name: routing-workbench-config
                  key: LISTEN_ADDR
            - name: OPENAI_BASE_URL
              valueFrom:
                configMapKeyRef:
                  name: routing-workbench-config
                  key: OPENAI_BASE_URL
            - name: WORKBENCH_DEFAULT_MODEL
              valueFrom:
                configMapKeyRef:
                  name: routing-workbench-config
                  key: WORKBENCH_DEFAULT_MODEL
            - name: WORKBENCH_REQUIRE_AUTH
              valueFrom:
                configMapKeyRef:
                  name: routing-workbench-config
                  key: WORKBENCH_REQUIRE_AUTH
            - name: WORKBENCH_AUTH_USER
              valueFrom:
                configMapKeyRef:
                  name: routing-workbench-config
                  key: WORKBENCH_AUTH_USER
            - name: WORKBENCH_AUTH_TOKEN
              valueFrom:
                secretKeyRef:
                  name: ${WORKBENCH_AUTH_SECRET}
                  key: token
                  optional: true
            - name: WORKBENCH_REQUEST_TIMEOUT_SECS
              valueFrom:
                configMapKeyRef:
                  name: routing-workbench-config
                  key: WORKBENCH_REQUEST_TIMEOUT_SECS
            - name: WORKBENCH_MAX_RUN_REQUESTS
              valueFrom:
                configMapKeyRef:
                  name: routing-workbench-config
                  key: WORKBENCH_MAX_RUN_REQUESTS
            - name: WORKBENCH_MAX_CONCURRENCY
              valueFrom:
                configMapKeyRef:
                  name: routing-workbench-config
                  key: WORKBENCH_MAX_CONCURRENCY
            - name: RUST_LOG
              valueFrom:
                configMapKeyRef:
                  name: routing-workbench-config
                  key: RUST_LOG
          resources:
            requests:
              cpu: 500m
              memory: 512Mi
            limits:
              cpu: "2"
              memory: 2Gi
          startupProbe:
            httpGet:
              path: /health
              port: http
            periodSeconds: 5
            timeoutSeconds: 5
            failureThreshold: 30
          readinessProbe:
            httpGet:
              path: /health
              port: http
            periodSeconds: 10
            timeoutSeconds: 5
            failureThreshold: 3
          livenessProbe:
            httpGet:
              path: /health
              port: http
            periodSeconds: 30
            timeoutSeconds: 5
            failureThreshold: 3
---
apiVersion: v1
kind: Service
metadata:
  name: routing-workbench
  labels:
    app.kubernetes.io/name: routing-workbench
    app.kubernetes.io/part-of: ${APP_LABEL_VALUE}
spec:
  type: ClusterIP
  selector:
    app.kubernetes.io/name: routing-workbench
  ports:
    - name: http
      port: 3000
      targetPort: 3000
---
apiVersion: v1
kind: Service
metadata:
  name: routing-workbench-np
  labels:
    app.kubernetes.io/name: routing-workbench
    app.kubernetes.io/part-of: ${APP_LABEL_VALUE}
spec:
  type: NodePort
  selector:
    app.kubernetes.io/name: routing-workbench
  ports:
    - name: http
      port: 3000
      targetPort: 3000
      nodePort: ${WORKBENCH_NODEPORT}
EOF
}

cleanup_legacy_photo_tagger_resources() {
  kubectl delete deployment photo-tagger \
    -n "${NAMESPACE}" \
    --ignore-not-found \
    --wait=false || true
  kubectl delete statefulset photo-tagger-postgres \
    -n "${NAMESPACE}" \
    --ignore-not-found \
    --wait=false || true
  kubectl delete service photo-tagger photo-tagger-np photo-tagger-postgres \
    -n "${NAMESPACE}" \
    --ignore-not-found \
    --wait=false || true
  kubectl delete configmap photo-tagger-config \
    -n "${NAMESPACE}" \
    --ignore-not-found \
    --wait=false || true
  kubectl delete networkpolicy photo-tagger-postgres-ingress \
    -n "${NAMESPACE}" \
    --ignore-not-found \
    --wait=false || true
}

cleanup_workbench_resources() {
  kubectl delete deployment routing-workbench \
    -n "${NAMESPACE}" \
    --ignore-not-found \
    --wait=false || true
  kubectl delete service routing-workbench routing-workbench-np \
    -n "${NAMESPACE}" \
    --ignore-not-found \
    --wait=false || true
  kubectl delete configmap routing-workbench-config \
    -n "${NAMESPACE}" \
    --ignore-not-found \
    --wait=false || true
}

kubectl_delete_namespaced() {
  kubectl delete "$@" \
    -n "${NAMESPACE}" \
    --ignore-not-found \
    --wait=false || true
}

namespaced_resource_available() {
  local resource="$1"
  kubectl api-resources --namespaced -o name 2>/dev/null | grep -Fxq "${resource}"
}

kubectl_delete_optional_namespaced() {
  local resource="$1"
  shift
  namespaced_resource_available "${resource}" || return 0
  kubectl_delete_namespaced "${resource}" "$@"
}

kubectl_delete_matching_namespaced() {
  local resource="$1"
  local pattern="$2"
  local -a names=()
  namespaced_resource_available "${resource}" || return 0
  mapfile -t names < <(kubectl get "${resource}" -n "${NAMESPACE}" -o name 2>/dev/null | grep -F "${pattern}" || true)
  ((${#names[@]})) || return 0
  kubectl_delete_namespaced "${names[@]}"
}

wait_for_delete() {
  local resource="$1"
  local name="$2"
  [[ "${DELETE_WAIT_SECONDS}" != "0" ]] || return 0
  kubectl wait --for=delete "${resource}/${name}" \
    -n "${NAMESPACE}" \
    --timeout="${DELETE_WAIT_SECONDS}s" \
    > /dev/null 2>&1 || true
}

filter_backend_manifest_for_delete() {
  local include_pvcs="$1"
  awk -v include_pvcs="${include_pvcs}" '
    function flush() {
      if (doc != "" && (include_pvcs == "1" || kind != "PersistentVolumeClaim")) {
        printf "%s", doc
      }
      doc = ""
      kind = ""
    }
    /^---[[:space:]]*$/ {
      flush()
      doc = $0 ORS
      next
    }
    {
      doc = doc $0 ORS
      if ($1 == "kind:") {
        kind = $2
      }
    }
    END { flush() }
  '
}

filter_backend_pvc_manifest() {
  awk '
    function flush() {
      if (doc != "" && kind == "PersistentVolumeClaim") {
        printf "%s", doc
      }
      doc = ""
      kind = ""
    }
    /^---[[:space:]]*$/ {
      flush()
      doc = $0 ORS
      next
    }
    {
      doc = doc $0 ORS
      if ($1 == "kind:") {
        kind = $2
      }
    }
    END { flush() }
  '
}

kubectl_delete_manifest() {
  kubectl delete -n "${NAMESPACE}" \
    --ignore-not-found \
    --wait=false \
    -f - || true
}

deleting_all_backends() {
  local backend
  local -A selected=()
  [[ "${#SELECTED_BACKENDS[@]}" -eq 5 ]] || return 1
  for backend in "${SELECTED_BACKENDS[@]}"; do
    selected["${backend}"]=1
  done
  [[ -n "${selected[nemotron]:-}" && -n "${selected[qwen35b]:-}" && -n "${selected[qwen27b]:-}" && -n "${selected[gemma431b]:-}" && -n "${selected[nemotronnano]:-}" ]]
}

delete_shared_resources() {
  kubectl_delete_optional_namespaced agentgatewayparameters.agentgateway.dev "${GATEWAY_NAME}-params"
  kubectl_delete_optional_namespaced gateways.gateway.networking.k8s.io "${GATEWAY_NAME}"
  kubectl_delete_optional_namespaced agentgatewaypolicies.agentgateway.dev daedalus-dynamo-model-header
  kubectl_delete_optional_namespaced httproutes.gateway.networking.k8s.io daedalus-dynamo
  kubectl_delete_namespaced deployment model-catalog
  kubectl_delete_namespaced service model-catalog
  kubectl_delete_namespaced configmap model-catalog-config
  cleanup_workbench_resources
  cleanup_legacy_photo_tagger_resources
}

delete_backend_resources() {
  local backend="$1"
  local dgd
  dgd="$(backend_dgd "${backend}")"

  echo "==> Deleting backend ${backend} (${dgd}) from namespace ${NAMESPACE}"
  render_backend "${backend}" | filter_backend_manifest_for_delete 0 | kubectl_delete_manifest

  # The Dynamo and Grove operators create child resources with mixed labels.
  # Delete the controller CRs first, then sweep both label families and the
  # generated service/RBAC names so a stuck finalizer does not leave pods alive.
  kubectl_delete_optional_namespaced inferencepools.inference.networking.k8s.io "${dgd}-pool"
  kubectl_delete_optional_namespaced podcliquesets.grove.io "${dgd}"
  kubectl_delete_optional_namespaced inferencepools.inference.networking.k8s.io \
    -l "nvidia.com/dynamo-graph-deployment-name=${dgd}"
  kubectl_delete_optional_namespaced podcliquesets.grove.io \
    -l "nvidia.com/dynamo-graph-deployment-name=${dgd}"
  kubectl_delete_optional_namespaced podcliques.grove.io \
    -l "nvidia.com/dynamo-graph-deployment-name=${dgd}"
  kubectl_delete_optional_namespaced podgangs.scheduler.grove.io \
    -l "app.kubernetes.io/part-of=${dgd}"
  kubectl_delete_optional_namespaced podgroups.scheduling.run.ai \
    -l "app.kubernetes.io/part-of=${dgd}"
  kubectl_delete_optional_namespaced dynamocomponentdeployments.nvidia.com \
    -l "nvidia.com/dynamo-graph-deployment-name=${dgd}"
  kubectl_delete_optional_namespaced dynamographdeploymentrequests.nvidia.com \
    -l "nvidia.com/dynamo-graph-deployment-name=${dgd}"
  kubectl_delete_optional_namespaced dynamomodels.nvidia.com \
    -l "nvidia.com/dynamo-graph-deployment-name=${dgd}"
  kubectl_delete_optional_namespaced dynamocheckpoints.nvidia.com \
    -l "nvidia.com/dynamo-graph-deployment-name=${dgd}"
  kubectl_delete_optional_namespaced dynamographdeploymentscalingadapters.nvidia.com \
    -l "nvidia.com/dynamo-graph-deployment-name=${dgd}"
  kubectl_delete_matching_namespaced dynamoworkermetadatas.nvidia.com "/${dgd}"

  kubectl_delete_namespaced all,configmap,secret,serviceaccount,role,rolebinding \
    -l "nvidia.com/dynamo-graph-deployment-name=${dgd}"
  kubectl_delete_namespaced all,configmap,secret,serviceaccount,role,rolebinding,poddisruptionbudgets.policy \
    -l "app.kubernetes.io/part-of=${dgd}"

  kubectl_delete_namespaced serviceaccount "${dgd}" "${dgd}-k8s-service-discovery"
  kubectl_delete_namespaced role "grove.io:pcs:${dgd}" "${dgd}-k8s-service-discovery-role"
  kubectl_delete_namespaced rolebinding "grove.io:pcs:${dgd}" "${dgd}-k8s-service-discovery-binding"
  kubectl_delete_namespaced secret "${dgd}-initc-sa-token-secret"

  wait_for_delete dynamographdeployments.nvidia.com "${dgd}"
  wait_for_delete podcliquesets.grove.io "${dgd}"
  wait_for_delete inferencepools.inference.networking.k8s.io "${dgd}-pool"
}

render_manifest() {
  local backend
  validate_serving_config
  render_gateway_resources
  render_model_catalog
  if workbench_enabled; then
    render_workbench
  fi
  for backend in "${SELECTED_BACKENDS[@]}"; do
    render_backend "${backend}"
  done
}

apply_deployment() {
  if [[ "${SKIP_DEPLOY:-0}" == "1" ]]; then
    build_images
    push_images
    echo "==> SKIP_DEPLOY=1 set; build/push completed without applying Kubernetes resources."
    return
  fi

  if [[ "${INSTALL_GATEWAY:-0}" == "1" ]]; then
    install_gateway
  fi

  if [[ "${RUN_DOCTOR}" == "1" ]]; then
    echo "==> RUN_DOCTOR=1: running cluster doctor before deploy"
    if ! "${REPO_ROOT}/scripts/cluster-doctor.sh"; then
      if [[ "${DOCTOR_STRICT}" == "1" ]]; then
        die "Cluster doctor reported failures and DOCTOR_STRICT=1. Aborting deploy."
      fi
      echo "WARN: cluster doctor reported failures; continuing (set DOCTOR_STRICT=1 to abort)." >&2
    fi
  fi

  preflight_cluster
  ensure_hf_token_secret
  if workbench_enabled; then
    ensure_workbench_auth_secret
  fi

  build_images
  push_images

  echo "==> Applying Daedalus Dynamo GAIE deployment to namespace ${NAMESPACE}"
  render_manifest | kubectl apply -n "${NAMESPACE}" -f -
  cleanup_legacy_photo_tagger_resources
  if ! workbench_enabled; then
    cleanup_workbench_resources
  fi

  echo
  echo "Done. Watch pods with:"
  echo "  kubectl get pods -n ${NAMESPACE} -w"
  echo
  echo "Port-forward the shared inference gateway:"
  echo "  kubectl port-forward -n ${NAMESPACE} svc/${GATEWAY_NAME} 8000:80"
  echo
  if workbench_enabled; then
    echo "Open the Dynamo Routing Workbench:"
    echo "  http://<any-node-ip>:${WORKBENCH_NODEPORT}/"
  else
    echo "Deploy your application separately and point it at:"
    echo "  http://${GATEWAY_NAME}.${NAMESPACE}.svc.cluster.local:80/v1"
  fi
}

delete_deployment() {
  local backend
  if deleting_all_backends; then
    echo "==> Deleting shared Daedalus Dynamo GAIE resources from namespace ${NAMESPACE}"
    delete_shared_resources
  else
    echo "==> Targeted delete selected; preserving shared gateway, model catalog, and workbench resources."
    echo "    Redeploy remaining backends to refresh shared routing and model catalog entries."
  fi

  for backend in "${SELECTED_BACKENDS[@]}"; do
    delete_backend_resources "${backend}"
  done

  if [[ "${DELETE_DATA:-0}" == "1" ]]; then
    echo "==> DELETE_DATA=1 set; deleting selected backend PVCs"
    for backend in "${SELECTED_BACKENDS[@]}"; do
      render_backend "${backend}" | filter_backend_pvc_manifest | kubectl_delete_manifest
    done
    if deleting_all_backends; then
      echo "==> All backends selected; deleting shared app secrets and legacy data"
      kubectl delete pvc postgres-data-photo-tagger-postgres-0 \
        -n "${NAMESPACE}" \
        --ignore-not-found \
        --wait=false || true
      kubectl delete secret "${WORKBENCH_AUTH_SECRET}" photo-tagger-auth photo-tagger-postgres "${HF_TOKEN_SECRET}" \
        -n "${NAMESPACE}" \
        --ignore-not-found \
        --wait=false || true
    else
      echo "==> Targeted delete selected; preserving shared app secrets."
    fi
  else
    echo "==> Preserving PVCs and secrets. Set DELETE_DATA=1 to remove selected backend PVCs; with all backends selected it also removes shared app secrets and legacy data."
  fi
}

select_backends

case "${ACTION}" in
  deploy)
    apply_deployment
    ;;
  delete)
    delete_deployment
    ;;
  render)
    render_manifest
    ;;
  install-gateway)
    install_gateway
    ;;
  doctor)
    exec "${REPO_ROOT}/scripts/cluster-doctor.sh"
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage >&2
    exit 1
    ;;
esac
