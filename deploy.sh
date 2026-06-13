#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
REPO_ROOT="$(pwd)"

ACTION="deploy"
declare -a REQUESTED_BACKENDS=()

if [[ "$#" -gt 0 ]]; then
  case "$1" in
    deploy|delete|render|install-gateway|-h|--help|help)
      ACTION="$1"
      shift
      ;;
  esac
fi

if [[ "$#" -gt 0 ]]; then
  REQUESTED_BACKENDS=("$@")
elif [[ -n "${BACKENDS:-}" ]]; then
  IFS=',' read -r -a REQUESTED_BACKENDS <<< "${BACKENDS}"
else
  REQUESTED_BACKENDS=("all")
fi

REGISTRY="${REGISTRY:-daedalus.ddns.me:5050/btuttle}"
if [[ -z "${IMAGE_TAG:-}" ]]; then
  IMAGE_TAG="$(date -u +%Y%m%d%H%M%S)"
fi

NAMESPACE="${NAMESPACE:-daedalus-dynamo}"
GATEWAY_NAME="${GATEWAY_NAME:-inference-gateway}"
HF_TOKEN_SECRET="${HF_TOKEN_SECRET:-hf-token-secret}"
POSTGRES_SECRET="${POSTGRES_SECRET:-photo-tagger-postgres}"
PHOTO_TAGGER_AUTH_SECRET="${PHOTO_TAGGER_AUTH_SECRET:-photo-tagger-auth}"
APP_LABEL_VALUE="daedalus-dynamo-demo"
APP_LABEL="app.kubernetes.io/part-of=${APP_LABEL_VALUE}"
PREFLIGHT_TIMEOUT_SECONDS="${PREFLIGHT_TIMEOUT_SECONDS:-20}"
PHOTO_TAGGER_NODEPORT="${PHOTO_TAGGER_NODEPORT:-30153}"
GATEWAY_REQUEST_TIMEOUT="${GATEWAY_REQUEST_TIMEOUT:-300s}"
KV_CACHE_BLOCK_SIZE="${KV_CACHE_BLOCK_SIZE:-16}"
KV_EVENTS_ENDPOINT="${KV_EVENTS_ENDPOINT:-tcp://*:5557}"
MODEL_CATALOG_IMAGE="${MODEL_CATALOG_IMAGE:-python:3.12-alpine}"
DYNAMO_VERSION="${DYNAMO_VERSION:-1.2.0}"
DYNAMO_EPP_IMAGE="${DYNAMO_EPP_IMAGE:-nvcr.io/nvidia/ai-dynamo/dynamo-frontend:${DYNAMO_VERSION}}"
DYNAMO_EPP_NIXL_DIR="${DYNAMO_EPP_NIXL_DIR:-/opt/dynamo/venv/lib/python3.12/site-packages/.nixl_cu12.mesonpy.libs}"
DYNAMO_EPP_NIXL_PLUGIN_DIR="${DYNAMO_EPP_NIXL_PLUGIN_DIR:-${DYNAMO_EPP_NIXL_DIR}/plugins}"
DYNAMO_EPP_STAGED_UCX_DIR="${DYNAMO_EPP_STAGED_UCX_DIR:-/opt/dynamo/epp-ucx}"
DYNAMO_EPP_LD_LIBRARY_PATH="${DYNAMO_EPP_LD_LIBRARY_PATH:-${DYNAMO_EPP_NIXL_DIR}:${DYNAMO_EPP_NIXL_PLUGIN_DIR}:${DYNAMO_EPP_STAGED_UCX_DIR}:${DYNAMO_EPP_STAGED_UCX_DIR}/ucx}"
EPP_STAGE_UCX_LIBS="${EPP_STAGE_UCX_LIBS:-1}"
EPP_ENABLE_DYN_DECODE="${EPP_ENABLE_DYN_DECODE:-1}"
EPP_NODE_HOSTNAMES="${EPP_NODE_HOSTNAMES:-}"

NEMOTRON_IMAGE_REPOSITORY="${NEMOTRON_IMAGE_REPOSITORY:-${REGISTRY}/nemotron-3-nano-omni}"
QWEN35B_IMAGE_REPOSITORY="${QWEN35B_IMAGE_REPOSITORY:-${REGISTRY}/qwen36-35b-dynamo}"
QWEN27B_IMAGE_REPOSITORY="${QWEN27B_IMAGE_REPOSITORY:-${REGISTRY}/qwen36-27b-dynamo}"
PHOTO_TAGGER_IMAGE_REPOSITORY="${PHOTO_TAGGER_IMAGE_REPOSITORY:-${REGISTRY}/photo-tagger}"

NEMOTRON_IMAGE="${NEMOTRON_IMAGE_REPOSITORY}:${IMAGE_TAG}"
QWEN35B_IMAGE="${QWEN35B_IMAGE_REPOSITORY}:${IMAGE_TAG}"
QWEN27B_IMAGE="${QWEN27B_IMAGE_REPOSITORY}:${IMAGE_TAG}"
PHOTO_TAGGER_IMAGE="${PHOTO_TAGGER_IMAGE_REPOSITORY}:${IMAGE_TAG}"

usage() {
  cat <<EOF
Usage: $0 [deploy|delete|render|install-gateway] [backend...]

Backends:
  nemotron     nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-FP8 on vLLM
  qwen35b      Qwen/Qwen3.6-35B-A3B-FP8 on vLLM
  qwen27b      Qwen/Qwen3.6-27B-FP8 on SGLang
  all          Deploy all backends

Examples:
  HF_TOKEN=... ./deploy.sh deploy qwen35b qwen27b
  BACKENDS=nemotron,qwen35b ./deploy.sh render
  INSTALL_GATEWAY=1 HF_TOKEN=... ./deploy.sh deploy all

Common environment:
  NAMESPACE                    default: daedalus-dynamo
  REGISTRY                     default: daedalus.ddns.me:5050/btuttle
  IMAGE_TAG                    default: UTC timestamp
  SKIP_BUILD=1                 skip docker builds
  SKIP_PUSH=1                  skip docker pushes
  SKIP_DEPLOY=1                build/push only
  INSTALL_GATEWAY=1            install/upgrade Gateway API, GAIE, and agentgateway first
  DYNAMO_VERSION=1.2.0         tag used for the default Dynamo EPP image
  DYNAMO_EPP_IMAGE=<image>     override the EPP image
  EPP_STAGE_UCX_LIBS=1         stage UCX runtime libs from the backend image for EPP
  EPP_ENABLE_DYN_DECODE=1      enable Dynamo KV-aware EPP scorer
  EPP_ENABLE_DYN_DECODE=0      disable only the dyn-decode scorer if EPP cannot initialize NIXL
  EPP_NODE_HOSTNAMES=a,b,c     optional comma-separated node hostnames for EPP pods
  DEPLOY_PHOTO_TAGGER=1        deploy the shared reference app; set 0 to deploy gateway/backends only
  DEFAULT_BACKEND=<backend>    Photo Tagger model backend; defaults to first selected backend
  DEFAULT_MODEL=<model-id>     Photo Tagger model; defaults from DEFAULT_BACKEND
  PHOTO_TAGGER_NODEPORT=30153  shared Photo Tagger UI NodePort
  KV_CACHE_BLOCK_SIZE=16       EPP and worker KV block/page size
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

warn_mutable_image_tag() {
  if [[ "${IMAGE_TAG}" == "latest" && "${ALLOW_LATEST:-0}" != "1" ]]; then
    echo "WARN: IMAGE_TAG=latest is mutable. Set ALLOW_LATEST=1 to acknowledge, or omit IMAGE_TAG for timestamp tag." >&2
  fi
}

normalize_backend() {
  case "${1,,}" in
    all) printf 'all\n' ;;
    nemotron|nemotron-omni|omni) printf 'nemotron\n' ;;
    qwen35b|qwen-35b|qwen36-35b|35b) printf 'qwen35b\n' ;;
    qwen27b|qwen-27b|qwen36-27b|27b|sglang) printf 'qwen27b\n' ;;
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
      SELECTED_BACKENDS=(nemotron qwen35b qwen27b)
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
      nemotron|qwen35b|qwen27b) ;;
      *) die "Internal backend normalization error: ${backend}" ;;
    esac
  done
}

backend_model() {
  case "$1" in
    nemotron) printf 'nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-FP8\n' ;;
    qwen35b) printf 'Qwen/Qwen3.6-35B-A3B-FP8\n' ;;
    qwen27b) printf 'Qwen/Qwen3.6-27B-FP8\n' ;;
  esac
}

backend_dgd() {
  case "$1" in
    nemotron) printf 'nemotron-omni-vllm-agg\n' ;;
    qwen35b) printf 'qwen36-35b-vllm-agg\n' ;;
    qwen27b) printf 'qwen36-27b-sglang-agg\n' ;;
  esac
}

backend_framework() {
  case "$1" in
    nemotron|qwen35b) printf 'vllm\n' ;;
    qwen27b) printf 'sglang\n' ;;
  esac
}

backend_worker_component() {
  case "$1" in
    nemotron|qwen35b) printf 'VllmWorker\n' ;;
    qwen27b) printf 'SGLangWorker\n' ;;
  esac
}

backend_storage_prefix() {
  case "$1" in
    nemotron) printf 'nemotron\n' ;;
    qwen35b) printf 'qwen35b\n' ;;
    qwen27b) printf 'qwen27b\n' ;;
  esac
}

backend_image() {
  case "$1" in
    nemotron) printf '%s\n' "${NEMOTRON_IMAGE}" ;;
    qwen35b) printf '%s\n' "${QWEN35B_IMAGE}" ;;
    qwen27b) printf '%s\n' "${QWEN27B_IMAGE}" ;;
  esac
}

backend_build_dir() {
  case "$1" in
    nemotron) printf '%s\n' "nemotron-3-nano-omni-dynamo" ;;
    qwen35b) printf '%s\n' "qwen-36-35b-dynamo" ;;
    qwen27b) printf '%s\n' "qwen-36-27b-dynamo" ;;
  esac
}

backend_build_arg() {
  case "$1" in
    nemotron) printf '%s\n' "${NEMOTRON_BASE_IMAGE:-${BASE_IMAGE:-}}" ;;
    qwen35b) printf '%s\n' "${QWEN35B_BASE_IMAGE:-${BASE_IMAGE:-}}" ;;
    qwen27b) printf '%s\n' "${QWEN27B_BASE_IMAGE:-${BASE_IMAGE:-}}" ;;
  esac
}

default_backend() {
  local backend="${DEFAULT_BACKEND:-${SELECTED_BACKENDS[0]}}"
  normalize_backend "${backend}"
}

default_model() {
  if [[ -n "${DEFAULT_MODEL:-}" ]]; then
    printf '%s\n' "${DEFAULT_MODEL}"
    return
  fi
  backend_model "$(default_backend)"
}

photo_tagger_image_delivery() {
  if [[ -n "${PHOTO_TAGGER_IMAGE_DELIVERY:-}" ]]; then
    printf '%s\n' "${PHOTO_TAGGER_IMAGE_DELIVERY}"
    return
  fi
  if [[ "$(default_backend)" == "qwen27b" ]]; then
    printf 'url\n'
  else
    printf 'data-url\n'
  fi
}

photo_tagger_enabled() {
  local value="${DEPLOY_PHOTO_TAGGER:-1}"
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

api_resources() {
  kubectl api-resources --verbs=list -o name 2> /dev/null || true
}

api_resource_exists() {
  local resource="$1"
  api_resources | grep -Fxq "${resource}"
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

ensure_photo_tagger_postgres_secret() {
  ensure_namespace

  if kubectl get secret "${POSTGRES_SECRET}" -n "${NAMESPACE}" > /dev/null 2>&1; then
    echo "==> Reusing existing ${POSTGRES_SECRET} in namespace ${NAMESPACE}"
    kubectl label secret "${POSTGRES_SECRET}" -n "${NAMESPACE}" "${APP_LABEL}" --overwrite > /dev/null
    return
  fi

  echo "==> Creating ${POSTGRES_SECRET} in namespace ${NAMESPACE}"
  kubectl create secret generic "${POSTGRES_SECRET}" \
    --from-literal=password="${POSTGRES_PASSWORD:-$(generate_secret_value)}" \
    -n "${NAMESPACE}" \
    --dry-run=client -o yaml | kubectl apply -f -
  kubectl label secret "${POSTGRES_SECRET}" -n "${NAMESPACE}" "${APP_LABEL}" --overwrite > /dev/null
}

ensure_photo_tagger_auth_secret() {
  ensure_namespace

  if kubectl get secret "${PHOTO_TAGGER_AUTH_SECRET}" -n "${NAMESPACE}" > /dev/null 2>&1; then
    echo "==> Reusing existing ${PHOTO_TAGGER_AUTH_SECRET} in namespace ${NAMESPACE}"
    kubectl label secret "${PHOTO_TAGGER_AUTH_SECRET}" -n "${NAMESPACE}" "${APP_LABEL}" --overwrite > /dev/null
    return
  fi

  echo "==> Creating ${PHOTO_TAGGER_AUTH_SECRET} in namespace ${NAMESPACE}"
  kubectl create secret generic "${PHOTO_TAGGER_AUTH_SECRET}" \
    --from-literal=token="${PHOTO_TAGGER_AUTH_TOKEN:-$(generate_secret_value)}" \
    -n "${NAMESPACE}" \
    --dry-run=client -o yaml | kubectl apply -f -
  kubectl label secret "${PHOTO_TAGGER_AUTH_SECRET}" -n "${NAMESPACE}" "${APP_LABEL}" --overwrite > /dev/null
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

  if photo_tagger_enabled; then
    echo "==> Building ${PHOTO_TAGGER_IMAGE}"
    docker build -t "${PHOTO_TAGGER_IMAGE}" \
      -f photo-tagger/Dockerfile \
      photo-tagger
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

  if photo_tagger_enabled; then
    echo "==> Pushing ${PHOTO_TAGGER_IMAGE}"
    docker push "${PHOTO_TAGGER_IMAGE}"
  fi
}

render_backend_storage() {
  local backend="$1"
  local prefix model_cache redis redis_data
  prefix="$(backend_storage_prefix "${backend}")"
  model_cache="${prefix}-model-cache"
  redis="${prefix}-redis"
  redis_data="${prefix}-redis-data"

  cat <<EOF
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${model_cache}
  labels:
    app.kubernetes.io/name: ${model_cache}
    app.kubernetes.io/part-of: ${APP_LABEL_VALUE}
    daedalus-dynamo/backend: ${backend}
spec:
  accessModes:
    - ReadWriteMany
  resources:
    requests:
      storage: ${MODEL_CACHE_STORAGE:-100Gi}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${redis_data}
  labels:
    app.kubernetes.io/name: ${redis}
    app.kubernetes.io/part-of: ${APP_LABEL_VALUE}
    daedalus-dynamo/backend: ${backend}
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 1Gi
---
apiVersion: v1
kind: Service
metadata:
  name: ${redis}
  labels:
    app.kubernetes.io/name: ${redis}
    app.kubernetes.io/part-of: ${APP_LABEL_VALUE}
    daedalus-dynamo/backend: ${backend}
spec:
  type: ClusterIP
  selector:
    app.kubernetes.io/name: ${redis}
  ports:
    - name: redis
      port: 6379
      targetPort: 6379
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${redis}
  labels:
    app.kubernetes.io/name: ${redis}
    app.kubernetes.io/part-of: ${APP_LABEL_VALUE}
    daedalus-dynamo/backend: ${backend}
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: ${redis}
  template:
    metadata:
      labels:
        app.kubernetes.io/name: ${redis}
        app.kubernetes.io/part-of: ${APP_LABEL_VALUE}
        daedalus-dynamo/backend: ${backend}
    spec:
      securityContext:
        fsGroup: 999
        fsGroupChangePolicy: OnRootMismatch
      containers:
        - name: redis
          image: redis:7-alpine
          imagePullPolicy: IfNotPresent
          securityContext:
            runAsUser: 999
            runAsGroup: 999
            runAsNonRoot: true
            allowPrivilegeEscalation: false
          args: ["--appendonly", "yes", "--dir", "/data"]
          ports:
            - name: redis
              containerPort: 6379
          resources:
            requests:
              cpu: 500m
              memory: 2Gi
            limits:
              cpu: "1"
              memory: 4Gi
          startupProbe:
            tcpSocket:
              port: 6379
            periodSeconds: 5
            failureThreshold: 30
          readinessProbe:
            tcpSocket:
              port: 6379
            initialDelaySeconds: 2
            periodSeconds: 5
          livenessProbe:
            tcpSocket:
              port: 6379
            periodSeconds: 10
          volumeMounts:
            - name: redis-data
              mountPath: /data
      volumes:
        - name: redis-data
          persistentVolumeClaim:
            claimName: ${redis_data}
EOF
}

render_epp_config() {
  cat <<'EOF'
      eppConfig:
        config:
          plugins:
            - type: disagg-profile-handler
            - name: decode-filter
              type: label-filter
              parameters:
                label: nvidia.com/dynamo-sub-component-type
                validValues:
                  - decode
                allowsNoLabel: true
            - name: picker
              type: max-score-picker
EOF

  if [[ "${EPP_ENABLE_DYN_DECODE,,}" != "0" && "${EPP_ENABLE_DYN_DECODE,,}" != "false" ]]; then
    cat <<'EOF'
            - name: dyn-decode
              type: dyn-decode-scorer
EOF
  fi

  cat <<'EOF'
          schedulingProfiles:
            - name: decode
              plugins:
                - pluginRef: decode-filter
                  weight: 1
EOF

  if [[ "${EPP_ENABLE_DYN_DECODE,,}" != "0" && "${EPP_ENABLE_DYN_DECODE,,}" != "false" ]]; then
    cat <<'EOF'
                - pluginRef: dyn-decode
                  weight: 1
EOF
  fi

  cat <<'EOF'
                - pluginRef: picker
                  weight: 1
EOF
}

render_dgd_header() {
  local backend="$1"
  local dgd framework model_cache
  dgd="$(backend_dgd "${backend}")"
  framework="$(backend_framework "${backend}")"
  model_cache="$(backend_storage_prefix "${backend}")-model-cache"

  cat <<EOF
---
apiVersion: nvidia.com/v1alpha1
kind: DynamoGraphDeployment
metadata:
  name: ${dgd}
  labels:
    app.kubernetes.io/name: ${dgd}
    app.kubernetes.io/part-of: ${APP_LABEL_VALUE}
    daedalus-dynamo/backend: ${backend}
spec:
  backendFramework: ${framework}
  pvcs:
    - name: ${model_cache}
      create: false
  services:
EOF
}

render_epp_node_affinity() {
  local host
  local -a hosts=()
  [[ -n "${EPP_NODE_HOSTNAMES}" ]] || return 0

  cat <<EOF
        affinity:
          nodeAffinity:
            requiredDuringSchedulingIgnoredDuringExecution:
              nodeSelectorTerms:
                - matchExpressions:
                    - key: kubernetes.io/hostname
                      operator: In
                      values:
EOF

  IFS=',' read -r -a hosts <<< "${EPP_NODE_HOSTNAMES}"
  for host in "${hosts[@]}"; do
    host="${host//[[:space:]]/}"
    [[ -n "${host}" ]] || continue
    cat <<EOF
                        - ${host}
EOF
  done
}

render_epp_ucx_staging() {
  local backend="$1"
  local image dgd
  [[ "${EPP_STAGE_UCX_LIBS,,}" != "0" && "${EPP_STAGE_UCX_LIBS,,}" != "false" ]] || return 0
  image="$(backend_image "${backend}")"
  dgd="$(backend_dgd "${backend}")"

  cat <<EOF
        initContainers:
          - name: stage-ucx-libs
            image: ${image}
            imagePullPolicy: IfNotPresent
            command:
              - /bin/bash
              - -lc
            args:
              - |
                set -euo pipefail
                src=""
                for candidate in \
                  /usr/local/lib/python3.12/dist-packages/nixl_cu12.libs \
                  /usr/local/lib/python3.12/dist-packages/nixl_cu13.libs \
                  /opt/dynamo/venv/lib/python3.12/site-packages/nixl_cu12.libs \
                  /opt/dynamo/venv/lib/python3.12/site-packages/nixl_cu13.libs; do
                  if [ -d "\${candidate}" ]; then
                    src="\${candidate}"
                    break
                  fi
                done
                if [ -z "\${src}" ]; then
                  echo "ERROR: no nixl_cu*.libs directory found in backend image" >&2
                  exit 1
                fi
                rm -rf /staged-ucx/*
                cp -a "\${src}/." /staged-ucx/
                for lib in libucp libucs libuct libucm; do
                  target="\$(find /staged-ucx -maxdepth 1 -name "\${lib}-*.so*" | sort | head -1 || true)"
                  if [ -n "\${target}" ]; then
                    ln -sf "\$(basename "\${target}")" "/staged-ucx/\${lib}.so.0"
                    ln -sf "\$(basename "\${target}")" "/staged-ucx/\${lib}.so"
                  fi
                done
            volumeMounts:
              - name: epp-ucx-libs
                mountPath: /staged-ucx
        volumes:
          - name: epp-config
            configMap:
              name: ${dgd}-epp-config
          - name: epp-ucx-libs
            emptyDir: {}
EOF
}

render_epp_service() {
  local backend="$1"
  local model image model_cache use_kv_events
  model="$(backend_model "${backend}")"
  image="${DYNAMO_EPP_IMAGE}"
  model_cache="$(backend_storage_prefix "${backend}")-model-cache"
  use_kv_events="true"
  if [[ "${EPP_ENABLE_DYN_DECODE,,}" == "0" || "${EPP_ENABLE_DYN_DECODE,,}" == "false" ]]; then
    use_kv_events="false"
  fi

  cat <<EOF
    Epp:
      componentType: epp
      envFromSecret: ${HF_TOKEN_SECRET}
      replicas: 1
      volumeMounts:
        - name: ${model_cache}
          mountPoint: /model
      extraPodSpec:
EOF
  render_epp_node_affinity
  render_epp_ucx_staging "${backend}"
  cat <<EOF
        mainContainer:
          image: ${image}
          imagePullPolicy: IfNotPresent
          command:
            - /bin/sh
            - -lc
            - |
              fixed_args=""
              for arg in "\$@"; do
                case "\$arg" in
                  -pool-name) arg=--pool-name ;;
                  -pool-namespace) arg=--pool-namespace ;;
                  -pool-group) arg=--pool-group ;;
                  -grpc-port) arg=--grpc-port ;;
                  -grpc-health-port) arg=--grpc-health-port ;;
                  -config-file) arg=--config-file ;;
                esac
                fixed_args="\${fixed_args} '\${arg}'"
              done
              eval "set -- \${fixed_args}"
              exec /epp "\$@"
            - epp-wrapper
          env:
            - name: LD_LIBRARY_PATH
              value: "${DYNAMO_EPP_LD_LIBRARY_PATH}"
            - name: NIXL_PLUGIN_DIR
              value: "${DYNAMO_EPP_NIXL_PLUGIN_DIR}"
            - name: DYN_MODEL_NAME
              value: ${model}
            - name: DYN_DECODE_FALLBACK
              value: "true"
            - name: DYN_ENFORCE_DISAGG
              value: "false"
            - name: DYN_KV_CACHE_BLOCK_SIZE
              value: "${KV_CACHE_BLOCK_SIZE}"
            - name: DYN_USE_KV_EVENTS
              value: "${use_kv_events}"
            - name: DYN_REQUEST_PLANE
              value: tcp
            - name: DYN_EVENT_PLANE
              value: zmq
            - name: DYN_TOKENIZER
              value: fastokens
            - name: HF_HUB_CACHE
              value: /model/.model-express/cache
EOF

  if [[ "${EPP_STAGE_UCX_LIBS,,}" != "0" && "${EPP_STAGE_UCX_LIBS,,}" != "false" ]]; then
    cat <<EOF
          volumeMounts:
            - name: epp-config
              mountPath: /etc/epp
              readOnly: true
            - name: epp-ucx-libs
              mountPath: ${DYNAMO_EPP_STAGED_UCX_DIR}
EOF
  fi

  render_epp_config
}

render_modelexpress_service() {
  local backend="$1"
  local model model_cache redis
  model="$(backend_model "${backend}")"
  model_cache="$(backend_storage_prefix "${backend}")-model-cache"
  redis="$(backend_storage_prefix "${backend}")-redis"

  cat <<EOF

    ModelExpress:
      componentType: frontend
      envFromSecret: ${HF_TOKEN_SECRET}
      replicas: 1
      livenessProbe:
        tcpSocket:
          port: 8000
        initialDelaySeconds: 30
        periodSeconds: 10
        timeoutSeconds: 5
        failureThreshold: 3
      readinessProbe:
        tcpSocket:
          port: 8000
        initialDelaySeconds: 10
        periodSeconds: 5
        timeoutSeconds: 3
        failureThreshold: 3
      volumeMounts:
        - name: ${model_cache}
          mountPoint: /model
      extraPodSpec:
        mainContainer:
          image: nvcr.io/nvidia/ai-dynamo/modelexpress-server:0.3.0
          imagePullPolicy: IfNotPresent
          env:
            - name: MODEL_EXPRESS_SERVER_PORT
              value: "8000"
            - name: MODEL_EXPRESS_LOG_LEVEL
              value: info
            - name: MODEL_EXPRESS_CACHE_DIRECTORY
              value: /model/.model-express/cache
            - name: MX_METADATA_BACKEND
              value: redis
            - name: MX_REDIS_HOST
              value: ${redis}
            - name: MX_REDIS_PORT
              value: "6379"
            - name: REDIS_URL
              value: redis://${redis}:6379
            - name: MODEL_NAME
              value: ${model}
            - name: MODEL_CACHE_PATH
              value: /model/.model-express/cache
            - name: HF_HUB_CACHE
              value: /model/.model-express/cache
            - name: HF_XET_HIGH_PERFORMANCE
              value: "1"
          command:
            - /bin/bash
            - -lc
          args:
            - |
              set -euo pipefail

              echo "Waiting for Redis metadata backend..."
              for i in \$(seq 1 60); do
                if timeout 1 bash -c "</dev/tcp/\${MX_REDIS_HOST}/\${MX_REDIS_PORT}" 2> /dev/null; then
                  echo "Redis is reachable."
                  break
                fi
                if [ "\$i" -eq 60 ]; then
                  echo "ERROR: Redis did not become reachable at \${MX_REDIS_HOST}:\${MX_REDIS_PORT}" >&2
                  exit 1
                fi
                echo "Waiting for Redis... (\$i/60)"
                sleep 2
              done

              echo "Starting Model Express Server..."
              ./modelexpress-server &

              SERVER_PID=\$!
              echo "Server started with PID: \$SERVER_PID"

              echo "Setting up Model Express configuration..."
              mkdir -p "\$MODEL_CACHE_PATH"
              mkdir -p /model/.model-express
              cat > /model/.model-express/config.yaml <<CONFIG_EOF
              local_path: \$MODEL_CACHE_PATH
              server_endpoint: http://localhost:8000
              timeout_secs: null
              CONFIG_EOF

              echo "Waiting for server to be ready..."
              for i in \$(seq 1 60); do
                if ./modelexpress-cli --endpoint http://localhost:8000 health > /dev/null 2>&1; then
                  echo "Server is ready!"
                  break
                fi
                echo "Waiting for server... (\$i/60)"
                sleep 2
              done

              echo "Downloading \$MODEL_NAME model..."
              ./modelexpress-cli --endpoint http://localhost:8000 \
                --cache-path "\$MODEL_CACHE_PATH" \
                model download "\$MODEL_NAME" --strategy server-only

              SNAPSHOTS_PARENT="\$MODEL_CACHE_PATH/models--\${MODEL_NAME//\\//--}/snapshots"
              SNAPSHOT_DIR="\$(ls -td "\$SNAPSHOTS_PARENT"/*/ 2>/dev/null | head -1 || true)"
              if [ -n "\$SNAPSHOT_DIR" ]; then
                SNAPSHOTS_PARENT=\$(dirname "\$SNAPSHOT_DIR")
                ln -sfn "\$(basename "\$SNAPSHOT_DIR")" "\$SNAPSHOTS_PARENT/latest"
                echo "Created symlink: latest -> \$(basename "\$SNAPSHOT_DIR")"
              fi

              echo "Server PID: \$SERVER_PID"
              wait "\$SERVER_PID"
EOF
}

render_frontend_sidecar() {
  local image="$1"
  local dgd="$2"
  cat <<EOF
        containers:
          - name: sidecar-frontend
            image: ${image}
            imagePullPolicy: IfNotPresent
            envFrom:
              - secretRef:
                  name: ${HF_TOKEN_SECRET}
            env:
              - name: DYN_HTTP_BODY_LIMIT_MB
                value: "400"
              - name: DYN_REQUEST_PLANE
                value: tcp
              - name: DYN_EVENT_PLANE
                value: zmq
              - name: DYN_TOKENIZER
                value: fastokens
              - name: DYN_COMPONENT
                value: frontend
              - name: DYN_DISCOVERY_BACKEND
                value: kubernetes
              - name: DYN_NAMESPACE
                value: ${NAMESPACE}-${dgd}
              - name: DYN_PARENT_DGD_K8S_NAME
                value: ${dgd}
              - name: DYN_PARENT_DGD_K8S_NAMESPACE
                value: ${NAMESPACE}
              - name: NATS_SERVER
                value: nats://dynamo-platform-nats.dynamo-system.svc.cluster.local:4222
              - name: POD_NAME
                valueFrom:
                  fieldRef:
                    fieldPath: metadata.name
              - name: POD_NAMESPACE
                valueFrom:
                  fieldRef:
                    fieldPath: metadata.namespace
              - name: POD_UID
                valueFrom:
                  fieldRef:
                    fieldPath: metadata.uid
            command:
              - python3
            args:
              - -m
              - dynamo.frontend
              - --router-mode
              - direct
              - --enable-anthropic-api
              - --strip-anthropic-preamble
              - --enable-streaming-tool-dispatch
              - --enable-streaming-reasoning-dispatch
              - --http-port
              - "8000"
            ports:
              - name: http
                containerPort: 8000
            startupProbe:
              httpGet:
                path: /health
                port: 8000
              periodSeconds: 10
              timeoutSeconds: 10
              failureThreshold: 180
            readinessProbe:
              httpGet:
                path: /health
                port: 8000
              periodSeconds: 10
              timeoutSeconds: 5
              failureThreshold: 6
EOF
}

render_vllm_worker() {
  local backend="$1"
  local component model image dgd model_cache modelexpress_service tool_parser reasoning_parser max_tokens max_len max_seqs
  component="$(backend_worker_component "${backend}")"
  model="$(backend_model "${backend}")"
  image="$(backend_image "${backend}")"
  dgd="$(backend_dgd "${backend}")"
  model_cache="$(backend_storage_prefix "${backend}")-model-cache"
  modelexpress_service="${dgd}-modelexpress"

  if [[ "${backend}" == "nemotron" ]]; then
    tool_parser="nemotron_nano"
    reasoning_parser="nemotron_nano"
    max_tokens="2560"
    max_len="65536"
    max_seqs="8"
  else
    tool_parser="qwen3_coder"
    reasoning_parser="qwen3"
    max_tokens="8192"
    max_len="128000"
    max_seqs="8"
  fi

  cat <<EOF

    ${component}:
      componentType: worker
      envFromSecret: ${HF_TOKEN_SECRET}
      replicas: 1
      resources:
        limits:
          gpu: "2"
        requests:
          gpu: "2"
      livenessProbe:
        httpGet:
          path: /live
          port: system
        periodSeconds: 10
        timeoutSeconds: 5
        failureThreshold: 6
      readinessProbe:
        httpGet:
          path: /health
          port: system
        periodSeconds: 10
        timeoutSeconds: 5
        failureThreshold: 3
      volumeMounts:
        - name: ${model_cache}
          mountPoint: /model
      sharedMemory:
        size: 64Gi
EOF
  cat <<EOF
      extraPodSpec:
EOF
  render_frontend_sidecar "${image}" "${dgd}"
  cat <<EOF
        affinity:
          nodeAffinity:
            requiredDuringSchedulingIgnoredDuringExecution:
              nodeSelectorTerms:
                - matchExpressions:
                    - key: nvidia.com/gpu.memory
                      operator: Gt
                      values:
                        - "40959"
          podAntiAffinity:
            requiredDuringSchedulingIgnoredDuringExecution:
              - labelSelector:
                  matchLabels:
                    nvidia.com/dynamo-graph-deployment-name: ${dgd}
                    nvidia.com/dynamo-component: ${component}
                topologyKey: kubernetes.io/hostname
        mainContainer:
          image: ${image}
          imagePullPolicy: IfNotPresent
          startupProbe:
            httpGet:
              path: /health
              port: 9090
            periodSeconds: 10
            timeoutSeconds: 10
            failureThreshold: 360
          env:
            - name: MODEL_NAME
              value: ${model}
            - name: HF_HUB_CACHE
              value: /model/.model-express/cache
            - name: MODEL_CACHE_PATH
              value: /model/.model-express/cache
            - name: MODEL_EXPRESS_URL
              value: http://${modelexpress_service}:8000
            - name: MODEL_EXPRESS_HOST
              value: ${modelexpress_service}
            - name: MODEL_EXPRESS_PORT
              value: "8000"
            - name: DYN_VLLM_EMBEDDING_TRANSFER_MODE
              value: local
            - name: DYN_MULTIMODAL_EMBEDDING_CACHE_GB
              value: "4"
            - name: DYN_MM_ALLOW_INTERNAL
              value: "1"
            - name: DYN_MM_VIDEO_NUM_FRAMES
              value: "256"
            - name: DYN_REQUEST_PLANE
              value: tcp
            - name: DYN_EVENT_PLANE
              value: zmq
            - name: DYN_HTTP_BODY_LIMIT_MB
              value: "400"
            - name: DYN_WORKER_NOFILE_LIMIT
              value: "65536"
            - name: DYN_KV_CACHE_BLOCK_SIZE
              value: "${KV_CACHE_BLOCK_SIZE}"
            - name: UCX_NET_DEVICES
              value: eth0
            - name: UCX_TLS
              value: tcp,cuda_copy,cuda_ipc
          command:
            - /bin/bash
            - -lc
          args:
            - |
              set -euo pipefail
              ulimit -n "\${DYN_WORKER_NOFILE_LIMIT}"
              ulimit -l unlimited

              echo "Waiting for ModelExpress at \${MODEL_EXPRESS_HOST}:\${MODEL_EXPRESS_PORT}..."
              for i in \$(seq 1 180); do
                if timeout 1 bash -c "</dev/tcp/\${MODEL_EXPRESS_HOST}/\${MODEL_EXPRESS_PORT}" 2> /dev/null; then
                  echo "ModelExpress is reachable."
                  break
                fi
                if [ "\$i" -eq 180 ]; then
                  echo "ERROR: ModelExpress did not become reachable at \${MODEL_EXPRESS_HOST}:\${MODEL_EXPRESS_PORT}" >&2
                  exit 1
                fi
                echo "Waiting for ModelExpress... (\$i/180)"
                sleep 2
              done

              cmd=(
                python3 -m dynamo.vllm
                --model "\${MODEL_NAME}"
                --served-model-name "\${MODEL_NAME}"
                --enable-multimodal
                --frontend-decoding
                --multimodal-embedding-cache-capacity-gb "\${DYN_MULTIMODAL_EMBEDDING_CACHE_GB}"
                --mm-processor-cache-gb 8
EOF
  if [[ "${backend}" == "nemotron" ]]; then
    cat <<'EOF'
                --media-io-kwargs '{"video":{"num_frames":256,"fps":1}}'
                --video-pruning-rate 0.5
EOF
  fi
  cat <<EOF
                --trust-remote-code
                --dyn-tool-call-parser ${tool_parser}
                --dyn-reasoning-parser ${reasoning_parser}
                --tensor-parallel-size 2
                --pipeline-parallel-size 1
                --data-parallel-size 1
                --gpu-memory-utilization 0.90
                --max-num-batched-tokens ${max_tokens}
                --max-model-len ${max_len}
                --max-num-seqs ${max_seqs}
                --enable-chunked-prefill
                --scheduling-policy priority
                --enable-prefix-caching
                --prefix-caching-hash-algo sha256
                --block-size "\${DYN_KV_CACHE_BLOCK_SIZE}"
                --kv-events-config '{"enable_kv_cache_events":true,"publisher":"zmq","topic":"kv-events","endpoint":"${KV_EVENTS_ENDPOINT}"}'
                --no-enable-log-requests
              )
              exec "\${cmd[@]}"
          securityContext:
            capabilities:
              add:
                - IPC_LOCK
                - SYS_RESOURCE
EOF
}

render_sglang_worker() {
  local backend="qwen27b"
  local component model image dgd model_cache modelexpress_service
  component="$(backend_worker_component "${backend}")"
  model="$(backend_model "${backend}")"
  image="$(backend_image "${backend}")"
  dgd="$(backend_dgd "${backend}")"
  model_cache="$(backend_storage_prefix "${backend}")-model-cache"
  modelexpress_service="${dgd}-modelexpress"

  cat <<EOF

    ${component}:
      componentType: worker
      envFromSecret: ${HF_TOKEN_SECRET}
      replicas: 1
      resources:
        limits:
          gpu: "2"
        requests:
          gpu: "2"
      livenessProbe:
        httpGet:
          path: /live
          port: system
        periodSeconds: 10
        timeoutSeconds: 5
        failureThreshold: 6
      readinessProbe:
        httpGet:
          path: /health
          port: system
        periodSeconds: 10
        timeoutSeconds: 5
        failureThreshold: 3
      volumeMounts:
        - name: ${model_cache}
          mountPoint: /model
      sharedMemory:
        size: 64Gi
EOF
  cat <<EOF
      extraPodSpec:
EOF
  render_frontend_sidecar "${image}" "${dgd}"
  cat <<EOF
        affinity:
          nodeAffinity:
            requiredDuringSchedulingIgnoredDuringExecution:
              nodeSelectorTerms:
                - matchExpressions:
                    - key: nvidia.com/gpu.memory
                      operator: Gt
                      values:
                        - "40959"
          podAntiAffinity:
            requiredDuringSchedulingIgnoredDuringExecution:
              - labelSelector:
                  matchLabels:
                    nvidia.com/dynamo-graph-deployment-name: ${dgd}
                    nvidia.com/dynamo-component: ${component}
                topologyKey: kubernetes.io/hostname
        mainContainer:
          image: ${image}
          imagePullPolicy: IfNotPresent
          startupProbe:
            httpGet:
              path: /health
              port: 9090
            periodSeconds: 10
            timeoutSeconds: 10
            failureThreshold: 360
          env:
            - name: MODEL_NAME
              value: ${model}
            - name: HF_HUB_CACHE
              value: /model/.model-express/cache
            - name: MODEL_CACHE_PATH
              value: /model/.model-express/cache
            - name: MODEL_EXPRESS_URL
              value: http://${modelexpress_service}:8000
            - name: MODEL_EXPRESS_HOST
              value: ${modelexpress_service}
            - name: MODEL_EXPRESS_PORT
              value: "8000"
            - name: DYN_REQUEST_PLANE
              value: tcp
            - name: DYN_EVENT_PLANE
              value: zmq
            - name: DYN_HTTP_BODY_LIMIT_MB
              value: "400"
            - name: DYN_WORKER_NOFILE_LIMIT
              value: "65536"
            - name: DYN_KV_CACHE_BLOCK_SIZE
              value: "${KV_CACHE_BLOCK_SIZE}"
            - name: UCX_NET_DEVICES
              value: eth0
            - name: UCX_TLS
              value: tcp,cuda_copy,cuda_ipc
          command:
            - /bin/bash
            - -lc
          args:
            - |
              set -euo pipefail
              ulimit -n "\${DYN_WORKER_NOFILE_LIMIT}"
              ulimit -l unlimited

              echo "Waiting for ModelExpress at \${MODEL_EXPRESS_HOST}:\${MODEL_EXPRESS_PORT}..."
              for i in \$(seq 1 180); do
                if timeout 1 bash -c "</dev/tcp/\${MODEL_EXPRESS_HOST}/\${MODEL_EXPRESS_PORT}" 2> /dev/null; then
                  echo "ModelExpress is reachable."
                  break
                fi
                if [ "\$i" -eq 180 ]; then
                  echo "ERROR: ModelExpress did not become reachable at \${MODEL_EXPRESS_HOST}:\${MODEL_EXPRESS_PORT}" >&2
                  exit 1
                fi
                echo "Waiting for ModelExpress... (\$i/180)"
                sleep 2
              done

              cmd=(
                python3 -m dynamo.sglang
                --model-path "\${MODEL_NAME}"
                --served-model-name "\${MODEL_NAME}"
                --tp-size 2
                --trust-remote-code
                --dyn-tool-call-parser qwen3_coder
                --dyn-reasoning-parser qwen3
                --mem-fraction-static 0.85
                --context-length 131072
                --chunked-prefill-size 8192
                --max-running-requests 8
                --schedule-policy lof
                --enable-priority-scheduling
                --page-size "\${DYN_KV_CACHE_BLOCK_SIZE}"
                --kv-events-config '{"publisher":"zmq","topic":"kv-events","endpoint":"${KV_EVENTS_ENDPOINT}"}'
                --enable-metrics
              )
              exec "\${cmd[@]}"
          securityContext:
            capabilities:
              add:
                - IPC_LOCK
                - SYS_RESOURCE
EOF
}

render_backend() {
  local backend="$1"
  render_backend_storage "${backend}"
  render_dgd_header "${backend}"
  render_epp_service "${backend}"
  render_modelexpress_service "${backend}"
  if [[ "${backend}" == "qwen27b" ]]; then
    render_sglang_worker
  else
    render_vllm_worker "${backend}"
  fi
}

render_model_catalog() {
  local catalog_json
  catalog_json="$(models_json)"

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
        - group: inference.networking.k8s.io
          kind: InferencePool
          name: ${dgd}-pool
          port: 8000
      timeouts:
        request: ${GATEWAY_REQUEST_TIMEOUT}
EOF
  done
}

render_photo_tagger() {
  local selected_model image_delivery
  selected_model="$(default_model)"
  image_delivery="$(photo_tagger_image_delivery)"

  cat <<EOF
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: photo-tagger-config
  labels:
    app.kubernetes.io/name: photo-tagger
    app.kubernetes.io/part-of: ${APP_LABEL_VALUE}
data:
  LISTEN_ADDR: 0.0.0.0:3000
  LLM_ENDPOINT: http://${GATEWAY_NAME}:80/v1/chat/completions
  MODEL_NAME: ${selected_model}
  IMAGE_DELIVERY: ${image_delivery}
  IMAGE_BASE_URL: http://photo-tagger:3000
  PHOTO_TAGGER_REQUIRE_AUTH: "${PHOTO_TAGGER_REQUIRE_AUTH:-true}"
  PHOTO_TAGGER_AUTH_USER: ${PHOTO_TAGGER_AUTH_USER:-photo-tagger}
  WRITE_SIDECAR: "${WRITE_SIDECAR:-true}"
  SIDECAR_ROOTS: ${SIDECAR_ROOTS:-/volume2/daedalus}
  ALLOWED_ROOTS: ${ALLOWED_ROOTS:-/volume1/homes,/volume2/daedalus}
  MAX_CONCURRENT_LLM: "${MAX_CONCURRENT_LLM:-4}"
  MAX_CONCURRENT_HASH: "${MAX_CONCURRENT_HASH:-8}"
  MAX_FILE_BYTES: "${MAX_FILE_BYTES:-104857600}"
  MAX_IMAGE_PIXELS: "${MAX_IMAGE_PIXELS:-80000000}"
  MAX_EDGE_PIXELS: "${MAX_EDGE_PIXELS:-2048}"
  DOWNSCALE_LARGE: "${DOWNSCALE_LARGE:-true}"
  LLM_TIMEOUT_SECS: "${LLM_TIMEOUT_SECS:-180}"
  LLM_MAX_RETRIES: "${LLM_MAX_RETRIES:-3}"
  JPEG_QUALITY: "${JPEG_QUALITY:-85}"
  RUST_LOG: ${RUST_LOG:-info,photo_tagger=info}
---
apiVersion: v1
kind: Service
metadata:
  name: photo-tagger-postgres
  labels:
    app.kubernetes.io/name: photo-tagger-postgres
    app.kubernetes.io/part-of: ${APP_LABEL_VALUE}
spec:
  selector:
    app.kubernetes.io/name: photo-tagger-postgres
  ports:
    - name: postgres
      port: 5432
      targetPort: 5432
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: photo-tagger-postgres
  labels:
    app.kubernetes.io/name: photo-tagger-postgres
    app.kubernetes.io/part-of: ${APP_LABEL_VALUE}
spec:
  serviceName: photo-tagger-postgres
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: photo-tagger-postgres
  template:
    metadata:
      labels:
        app.kubernetes.io/name: photo-tagger-postgres
        app.kubernetes.io/part-of: ${APP_LABEL_VALUE}
    spec:
      securityContext:
        fsGroup: 988
        fsGroupChangePolicy: OnRootMismatch
      containers:
        - name: postgres
          image: postgres:16-alpine
          imagePullPolicy: IfNotPresent
          securityContext:
            runAsUser: 977
            runAsGroup: 988
            runAsNonRoot: true
            allowPrivilegeEscalation: false
          ports:
            - name: postgres
              containerPort: 5432
          env:
            - name: POSTGRES_DB
              value: photo_tagger
            - name: POSTGRES_USER
              value: photo_tagger
            - name: POSTGRES_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: ${POSTGRES_SECRET}
                  key: password
            - name: PGDATA
              value: /var/lib/postgresql/data/pgdata
          startupProbe:
            exec:
              command:
                - sh
                - -c
                - pg_isready -U "\$POSTGRES_USER" -d "\$POSTGRES_DB"
            periodSeconds: 5
            timeoutSeconds: 5
            failureThreshold: 60
          readinessProbe:
            exec:
              command:
                - sh
                - -c
                - pg_isready -U "\$POSTGRES_USER" -d "\$POSTGRES_DB"
            periodSeconds: 10
            timeoutSeconds: 5
            failureThreshold: 6
          livenessProbe:
            exec:
              command:
                - sh
                - -c
                - pg_isready -U "\$POSTGRES_USER" -d "\$POSTGRES_DB"
            initialDelaySeconds: 60
            periodSeconds: 30
            timeoutSeconds: 5
            failureThreshold: 6
          resources:
            requests:
              cpu: 500m
              memory: 512Mi
            limits:
              cpu: "2"
              memory: 2Gi
          volumeMounts:
            - name: postgres-data
              mountPath: /var/lib/postgresql/data
  volumeClaimTemplates:
    - metadata:
        name: postgres-data
        labels:
          app.kubernetes.io/name: photo-tagger-postgres
          app.kubernetes.io/part-of: ${APP_LABEL_VALUE}
      spec:
        accessModes:
          - ReadWriteOnce
        resources:
          requests:
            storage: ${PHOTO_TAGGER_POSTGRES_STORAGE:-20Gi}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: photo-tagger
  labels:
    app.kubernetes.io/name: photo-tagger
    app.kubernetes.io/part-of: ${APP_LABEL_VALUE}
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app.kubernetes.io/name: photo-tagger
  template:
    metadata:
      labels:
        app.kubernetes.io/name: photo-tagger
        app.kubernetes.io/part-of: ${APP_LABEL_VALUE}
      annotations:
        photo-tagger/deployment-profile: daedalus
    spec:
      securityContext:
        runAsUser: 977
        runAsGroup: 988
        runAsNonRoot: true
        fsGroup: 988
        fsGroupChangePolicy: OnRootMismatch
        seccompProfile:
          type: RuntimeDefault
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
              - matchExpressions:
                  - key: kubernetes.io/hostname
                    operator: In
                    values:
                      - daedalus-02
                      - daedalus-03
                      - daedalus-04
      containers:
        - name: photo-tagger
          image: ${PHOTO_TAGGER_IMAGE}
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
                  name: photo-tagger-config
                  key: LISTEN_ADDR
            - name: LLM_ENDPOINT
              valueFrom:
                configMapKeyRef:
                  name: photo-tagger-config
                  key: LLM_ENDPOINT
            - name: MODEL_NAME
              valueFrom:
                configMapKeyRef:
                  name: photo-tagger-config
                  key: MODEL_NAME
            - name: IMAGE_DELIVERY
              valueFrom:
                configMapKeyRef:
                  name: photo-tagger-config
                  key: IMAGE_DELIVERY
            - name: IMAGE_BASE_URL
              valueFrom:
                configMapKeyRef:
                  name: photo-tagger-config
                  key: IMAGE_BASE_URL
            - name: PHOTO_TAGGER_REQUIRE_AUTH
              valueFrom:
                configMapKeyRef:
                  name: photo-tagger-config
                  key: PHOTO_TAGGER_REQUIRE_AUTH
            - name: PHOTO_TAGGER_AUTH_USER
              valueFrom:
                configMapKeyRef:
                  name: photo-tagger-config
                  key: PHOTO_TAGGER_AUTH_USER
            - name: PHOTO_TAGGER_AUTH_TOKEN
              valueFrom:
                secretKeyRef:
                  name: ${PHOTO_TAGGER_AUTH_SECRET}
                  key: token
                  optional: true
            - name: POSTGRES_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: ${POSTGRES_SECRET}
                  key: password
            - name: DATABASE_URL
              value: postgres://photo_tagger:\$(POSTGRES_PASSWORD)@photo-tagger-postgres:5432/photo_tagger
            - name: WRITE_SIDECAR
              valueFrom:
                configMapKeyRef:
                  name: photo-tagger-config
                  key: WRITE_SIDECAR
            - name: SIDECAR_ROOTS
              valueFrom:
                configMapKeyRef:
                  name: photo-tagger-config
                  key: SIDECAR_ROOTS
            - name: ALLOWED_ROOTS
              valueFrom:
                configMapKeyRef:
                  name: photo-tagger-config
                  key: ALLOWED_ROOTS
            - name: MAX_CONCURRENT_LLM
              valueFrom:
                configMapKeyRef:
                  name: photo-tagger-config
                  key: MAX_CONCURRENT_LLM
            - name: MAX_CONCURRENT_HASH
              valueFrom:
                configMapKeyRef:
                  name: photo-tagger-config
                  key: MAX_CONCURRENT_HASH
            - name: MAX_FILE_BYTES
              valueFrom:
                configMapKeyRef:
                  name: photo-tagger-config
                  key: MAX_FILE_BYTES
            - name: MAX_IMAGE_PIXELS
              valueFrom:
                configMapKeyRef:
                  name: photo-tagger-config
                  key: MAX_IMAGE_PIXELS
            - name: MAX_EDGE_PIXELS
              valueFrom:
                configMapKeyRef:
                  name: photo-tagger-config
                  key: MAX_EDGE_PIXELS
            - name: DOWNSCALE_LARGE
              valueFrom:
                configMapKeyRef:
                  name: photo-tagger-config
                  key: DOWNSCALE_LARGE
            - name: LLM_TIMEOUT_SECS
              valueFrom:
                configMapKeyRef:
                  name: photo-tagger-config
                  key: LLM_TIMEOUT_SECS
            - name: LLM_MAX_RETRIES
              valueFrom:
                configMapKeyRef:
                  name: photo-tagger-config
                  key: LLM_MAX_RETRIES
            - name: JPEG_QUALITY
              valueFrom:
                configMapKeyRef:
                  name: photo-tagger-config
                  key: JPEG_QUALITY
            - name: RUST_LOG
              valueFrom:
                configMapKeyRef:
                  name: photo-tagger-config
                  key: RUST_LOG
          resources:
            requests:
              cpu: "4"
              memory: 4Gi
            limits:
              cpu: "8"
              memory: 8Gi
          startupProbe:
            exec:
              command: ["wget", "-qO-", "--timeout=3", "http://127.0.0.1:3000/health"]
            periodSeconds: 5
            timeoutSeconds: 5
            failureThreshold: 60
          readinessProbe:
            exec:
              command: ["wget", "-qO-", "--timeout=3", "http://127.0.0.1:3000/ready"]
            periodSeconds: 10
            timeoutSeconds: 5
            failureThreshold: 6
          livenessProbe:
            exec:
              command: ["sh", "-c", "kill -0 1"]
            periodSeconds: 30
            timeoutSeconds: 5
            failureThreshold: 3
          volumeMounts:
            - name: tmp
              mountPath: /tmp
            - name: data-photos
              mountPath: /data/photos
            - name: volume1-homes
              mountPath: /volume1/homes
              readOnly: true
            - name: volume2-daedalus
              mountPath: /volume2/daedalus
              readOnly: false
      volumes:
        - name: tmp
          emptyDir: {}
        - name: data-photos
          emptyDir: {}
        - name: volume1-homes
          nfs:
            server: ${VOLUME1_NFS_SERVER:-192.168.1.232}
            path: ${VOLUME1_NFS_PATH:-/volume1/homes}
            readOnly: true
        - name: volume2-daedalus
          nfs:
            server: ${VOLUME2_NFS_SERVER:-192.168.1.77}
            path: ${VOLUME2_NFS_PATH:-/var/nfs/shared/daedalus}
---
apiVersion: v1
kind: Service
metadata:
  name: photo-tagger
  labels:
    app.kubernetes.io/name: photo-tagger
    app.kubernetes.io/part-of: ${APP_LABEL_VALUE}
spec:
  type: ClusterIP
  selector:
    app.kubernetes.io/name: photo-tagger
  ports:
    - name: http
      port: 3000
      targetPort: 3000
---
apiVersion: v1
kind: Service
metadata:
  name: photo-tagger-np
  labels:
    app.kubernetes.io/name: photo-tagger
    app.kubernetes.io/part-of: ${APP_LABEL_VALUE}
spec:
  type: NodePort
  selector:
    app.kubernetes.io/name: photo-tagger
  ports:
    - name: http
      port: 3000
      targetPort: 3000
      nodePort: ${PHOTO_TAGGER_NODEPORT}
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: photo-tagger-postgres-ingress
  labels:
    app.kubernetes.io/name: photo-tagger-postgres
    app.kubernetes.io/part-of: ${APP_LABEL_VALUE}
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: photo-tagger-postgres
  policyTypes:
    - Ingress
  ingress:
    - from:
        - podSelector:
            matchLabels:
              app.kubernetes.io/name: photo-tagger
      ports:
        - protocol: TCP
          port: 5432
EOF
}

render_manifest() {
  local backend
  render_gateway_resources
  render_model_catalog
  if photo_tagger_enabled; then
    render_photo_tagger
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

  preflight_cluster
  ensure_hf_token_secret
  if photo_tagger_enabled; then
    ensure_photo_tagger_postgres_secret
    ensure_photo_tagger_auth_secret
  fi

  build_images
  push_images

  echo "==> Applying Daedalus Dynamo GAIE deployment to namespace ${NAMESPACE}"
  render_manifest | kubectl apply -n "${NAMESPACE}" -f -

  echo
  echo "Done. Watch pods with:"
  echo "  kubectl get pods -n ${NAMESPACE} -w"
  echo
  echo "Port-forward the shared inference gateway:"
  echo "  kubectl port-forward -n ${NAMESPACE} svc/${GATEWAY_NAME} 8000:80"
  echo
  if photo_tagger_enabled; then
    echo "Open the shared Photo Tagger UI:"
    echo "  http://<any-node-ip>:${PHOTO_TAGGER_NODEPORT}/"
  else
    echo "Deploy your application separately and point it at:"
    echo "  http://${GATEWAY_NAME}.${NAMESPACE}.svc.cluster.local:80/v1"
  fi
}

delete_deployment() {
  echo "==> Deleting rendered Daedalus Dynamo GAIE resources from namespace ${NAMESPACE}"
  render_manifest | kubectl delete -n "${NAMESPACE}" -f - --ignore-not-found --wait=false || true

  local backend dgd
  for backend in "${SELECTED_BACKENDS[@]}"; do
    dgd="$(backend_dgd "${backend}")"
    kubectl delete dynamographdeployment.nvidia.com "${dgd}" \
      -n "${NAMESPACE}" \
      --ignore-not-found \
      --wait=false || true
    kubectl delete all,configmap,serviceaccount,role,rolebinding \
      -n "${NAMESPACE}" \
      -l "nvidia.com/dynamo-graph-deployment-name=${dgd}" \
      --ignore-not-found \
      --wait=false || true
  done

  if [[ "${DELETE_DATA:-0}" == "1" ]]; then
    echo "==> DELETE_DATA=1 set; deleting selected PVCs and app secrets"
    for backend in "${SELECTED_BACKENDS[@]}"; do
      kubectl delete pvc \
        "$(backend_storage_prefix "${backend}")-model-cache" \
        "$(backend_storage_prefix "${backend}")-redis-data" \
        -n "${NAMESPACE}" \
        --ignore-not-found \
        --wait=false || true
    done
    kubectl delete pvc postgres-data-photo-tagger-postgres-0 \
      -n "${NAMESPACE}" \
      --ignore-not-found \
      --wait=false || true
    kubectl delete secret "${POSTGRES_SECRET}" "${PHOTO_TAGGER_AUTH_SECRET}" "${HF_TOKEN_SECRET}" \
      -n "${NAMESPACE}" \
      --ignore-not-found \
      --wait=false || true
  else
    echo "==> Preserving PVCs and secrets. Set DELETE_DATA=1 to remove model caches, Redis data, Postgres data, and app secrets."
  fi
}

select_backends

case "${ACTION}" in
  deploy)
    warn_mutable_image_tag
    apply_deployment
    ;;
  delete)
    delete_deployment
    ;;
  render)
    warn_mutable_image_tag
    render_manifest
    ;;
  install-gateway)
    install_gateway
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage >&2
    exit 1
    ;;
esac
